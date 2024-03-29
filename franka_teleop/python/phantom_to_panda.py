#! /usr/bin/python
from absl import app
from absl import flags

import rospy
import numpy as np
import actionlib
import franka_gripper.msg
from franka_gripper.msg import GraspEpsilon
import franka_msgs.msg
from scipy.spatial.transform import Rotation as R
from omni_msgs.msg import OmniState, OmniButtonEvent, OmniFeedback
from geometry_msgs.msg import Pose, PoseStamped, Point, Quaternion, WrenchStamped
from jsk_rviz_plugins.msg import OverlayText
from std_msgs.msg import Header, Bool
from franka_example_controllers.msg import ArmsTargetPose
from franka_teleop.srv import *

FLAGS = flags.FLAGS

flags.DEFINE_float(
    'vel_scale', 1.5, 'Velocicy scale of master/slave. Larger value means more movement in robot.')

flags.DEFINE_bool(
    'connect_pose', True, 'Set this to connect TouchUSB and Panda position command from start. Otherwise you have to call rosservice.')
flags.DEFINE_bool(
    'connect_force', True, 'Set this to connect TouchUSB and Panda force command from start. Otherwise you have to call rosservice.')


def get_status_overlay(moving, arm_name):
    text = OverlayText()
    text.width = 400
    text.height = 600
    text.left = 10 if arm_name == 'larm' else 600
    text.top = 10
    text.text_size = 25
    text.line_width = 2
    text.font = "DejaVu Sans Mono"
    on_txt = """<span style="color: red;">ON</span>"""
    off_txt = """<span style="color: white;">OFF</span>"""
    on_or_off = on_txt if moving else off_txt
    text.text = "TouchUSB control {}".format(on_or_off)
    return text


def get_initial_pose():
    r = PoseStamped(header=Header(frame_id='rarm_link0'),
                    pose=Pose(position=Point(0.3, 0.0, 0.48),
                              orientation=Quaternion(1.0, 0.0, 0.0, 0.0)))
    l = PoseStamped(header=Header(frame_id='larm_link0'),
                    pose=Pose(position=Point(0.3, 0.0, 0.48),
                              orientation=Quaternion(1.0, 0.0, 0.0, 0.0)))
    return r, l


def get_initial_pose_single(arm_name):
    return PoseStamped(header=Header(frame_id='{}_link0'.format(arm_name)),
                       pose=Pose(position=Point(0.3, 0.0, 0.48),
                                 orientation=Quaternion(1.0, 0.0, 0.0, 0.0)))


class SingleArmHandler(object):

    def __init__(self, robot_id='dual_panda', arm_name='larm', master_dev_name='left_device', vel_scale=1.0, force_scale=0.1, send_rot=True):
        self.robot_id = robot_id
        self.arm_name = arm_name
        self.side = 'left' if arm_name == 'larm' else 'right'
        self.master_dev_name = master_dev_name
        self.vel_scale = 0.001 * 0.001 * vel_scale  # [1000Hz] * [m/mm] * [1.0 (scaler)]
        self.force_scale = force_scale
        self.send_rot = send_rot
        master_initial_q = R.from_quat([-0.5, -0.5, -0.5, 0.5])   # initial rotation of the TouchUSB
        panda_initial_q = R.from_quat([1.0, 0, 0, 0])             # initial rotation of the Panda Arm
        self.q_m_p = panda_initial_q * master_initial_q.inv() # conversion from TouchUSB to Panda
        self.target_force = OmniFeedback()
        self.zero_force = None  # for initializatoin
        self.current_frame = None        
        self.is_gripper_closed = False
        self.has_error = False
        self.moving = False
        self.setup_ros()

        # open gripper
        self.open_gripper()

    def setup_ros(self):
        current_frame_topic = '/{}/dual_arm_cartesian_pose_controller/{}_frame'.format(self.robot_id, self.side)
        dev_topic = "/{}/phantom/state".format(self.master_dev_name)
        force_topic = "{}/{}_state_controller/F_ext".format(self.robot_id, self.arm_name)
        force_pub_topic = "/{}/phantom/force_feedback".format(self.master_dev_name)
        pose_target_pub_topic = '/{}/dual_arm_cartesian_pose_controller/{}_target_pose'.format(self.robot_id, self.arm_name)
        gripper_topic = '/{}/{}/franka_gripper/grasp'.format(self.robot_id, self.arm_name)
        error_topic = '/{}/{}/has_error'.format(self.robot_id, self.arm_name)
        status_overlay_topic = '/{}/{}/status_overlay'.format(self.robot_id, self.arm_name)
        self.current_arm_frame_sub = rospy.Subscriber(current_frame_topic, PoseStamped, self.current_frame_cb)
        rospy.wait_for_message(current_frame_topic, PoseStamped)

        self.device_state_sub = rospy.Subscriber(dev_topic, OmniState, self.device_state_sub)
        self.panda_force_sub = rospy.Subscriber(force_topic, WrenchStamped, self.force_cb)
        self.error_topic = rospy.Subscriber(error_topic, Bool, self.has_error_cb)
        self.target_pose = self.set_initial_pose(no_ask=True)
        
        self.single_target_pose_pub = rospy.Publisher(pose_target_pub_topic, PoseStamped, queue_size=1)
        self.force_feedback_pub = rospy.Publisher(force_pub_topic, OmniFeedback, queue_size=1)
        self.status_overlay_pub = rospy.Publisher(status_overlay_topic, OverlayText, queue_size=1)
        self.gripper_client = actionlib.SimpleActionClient(gripper_topic, franka_gripper.msg.GraspAction)
        self.gripper_client.wait_for_server()

    def current_frame_cb(self, msg):
        self.current_frame = msg
        
    def device_state_sub(self, msg):
        # control gripper
        if msg.close_gripper and (not self.is_gripper_closed):
            rospy.loginfo("Close {} gripper".format(self.arm_name))
            self.close_gripper()
            self.is_gripper_closed = True
        if (not msg.close_gripper) and self.is_gripper_closed:
            rospy.loginfo("Open {} gripper".format(self.arm_name))
            self.open_gripper()
            self.is_gripper_closed = False
        # display status
        self.display_status()            
        if msg.locked:  # not move target pose when locked
            self.moving = False
            return
        # update self.target_pose
        self.moving = True        
        self.target_pose.pose.position.x += msg.velocity.y * self.vel_scale
        self.target_pose.pose.position.y += -msg.velocity.x * self.vel_scale
        self.target_pose.pose.position.z += msg.velocity.z * self.vel_scale
        if self.send_rot:
            cur_q = R.from_quat([msg.pose.orientation.x,
                                 msg.pose.orientation.y,
                                 msg.pose.orientation.z,
                                 msg.pose.orientation.w])
            tar_q = self.q_m_p * cur_q    # apply conversion
            self.target_pose.pose.orientation.x = -tar_q.as_quat()[0]
            self.target_pose.pose.orientation.y = tar_q.as_quat()[1]
            self.target_pose.pose.orientation.z = -tar_q.as_quat()[2]
            self.target_pose.pose.orientation.w = tar_q.as_quat()[3]
        # publish target pose
        self.single_target_pose_pub.publish(self.target_pose)

    def force_cb(self, msg):
        if self.zero_force is None:
            self.zero_force = np.array([msg.wrench.force.x,
                                              msg.wrench.force.y,
                                              msg.wrench.force.z])
        self.target_force.force.x = (msg.wrench.force.y - self.zero_force[0]) * self.force_scale
        self.target_force.force.y = -(msg.wrench.force.x - self.zero_force[1]) * self.force_scale
        self.target_force.force.z = (msg.wrench.force.z - self.zero_force[2]) * self.force_scale

    def apply_target_force(self):
        self.force_feedback_pub.publish(self.target_force)

    def set_initial_pose(self, no_ask=False):
        if not no_ask:
            res = raw_input('Press Enter to set this {} configuration as initial position [Enter]'.format(self.arm_name))
        self.current_frame.header.frame_id = '{}_link0'.format(self.arm_name)
        return self.current_frame

    def open_gripper(self):
        # TODO
        epsilon = GraspEpsilon(inner=0.01, outer=0.01)
        goal = franka_gripper.msg.GraspGoal(width=0.08, speed=1, force=10, epsilon=epsilon)
        self.gripper_client.send_goal(goal)

    def close_gripper(self):
        # TODO, currently max force 140[N] is applied
        epsilon = GraspEpsilon(inner=0.01, outer=0.01)
        goal = franka_gripper.msg.GraspGoal(width=0, speed=1, force=140, epsilon=epsilon)
        self.gripper_client.send_goal(goal)

    def has_error_cb(self, msg):
        self.has_error = msg.data

    def display_status(self):
        msg = get_status_overlay(moving=self.moving, arm_name=self.arm_name)
        self.status_overlay_pub.publish(msg)
        
    def __del__(self):
        rospy.loginfo("Exiting {} handelr".format(self.arm_name))


class DualArmHandler(object):

    def __init__(self, vel_scale=1.0, pose_connecting=False, force_connecting=False):
        rospy.init_node('DualPhantomMaster')
        self.pose_connecting = pose_connecting
        self.force_connecting = force_connecting        
        self.rarm_handler = SingleArmHandler(arm_name='rarm', master_dev_name='right_device', vel_scale=vel_scale)
        self.larm_handler = SingleArmHandler(arm_name='larm', master_dev_name='left_device', vel_scale=vel_scale)
        self.arms = [self.rarm_handler, self.larm_handler]
        self.target_pub = rospy.Publisher('/dual_panda/dual_arm_cartesian_pose_controller/arms_target_pose', ArmsTargetPose, queue_size=1)
        self.recover_error_server = actionlib.SimpleActionClient('/dual_panda/error_recovery', franka_msgs.msg.ErrorRecoveryAction)
        r_initial_pose, l_initial_pose = self.set_initial_pose()
        self.target_pose = ArmsTargetPose(right_target=r_initial_pose, left_target=l_initial_pose)
        self.control_bilateral_srv = rospy.Service('/dual_panda/control_bilateral', ControlBilateral, self.control_bilateral)

    def control_bilateral(self, req):
        rospy.loginfo("Applying bilateral connection status, changing in {}...".format(req.wait))
        rospy.sleep(req.wait)
        self.pose_connecting =  req.pose_connecting
        self.force_connecting = req.force_connecting
        if req.reset_phantom:
            r_initial_pose, l_initial_pose = self.set_initial_pose()
            self.target_pose = ArmsTargetPose(right_target=r_initial_pose, left_target=l_initial_pose)
        return ControlBilateralResponse(True)
        
    def set_initial_pose(self):
        return self.rarm_handler.set_initial_pose(no_ask=True), self.larm_handler.set_initial_pose(no_ask=True)

    def loop_call(self):
        # send target pose for both arm if connected
        if self.pose_connecting:
            self.target_pose.right_target = self.rarm_handler.target_pose
            self.target_pose.left_target = self.larm_handler.target_pose
            self.target_pub.publish(self.target_pose)
        else:
            self.rarm_handler.target_pose = self.rarm_handler.set_initial_pose(no_ask=True)
            self.larm_handler.target_pose = self.larm_handler.set_initial_pose(no_ask=True)

        # apply force feedback if connected
        if self.force_connecting:
            for arm in self.arms:
                arm.apply_target_force()
        # recover error
        if self.rarm_handler.has_error or self.larm_handler.has_error:
            rospy.loginfo("Detected error in controller, recovering...")
            recover_goal = franka_msgs.msg.ErrorRecoveryActionGoal()
            self.recover_error_server.send_goal(recover_goal)

    def run(self):
        rospy.loginfo("Start looping")
        r = rospy.Rate(50)
        while not rospy.is_shutdown():
            self.loop_call()
            r.sleep()

    def __del__(self):
        rospy.loginfo("Exiting phantom master node")


def main(argv):
    node = DualArmHandler(vel_scale=FLAGS.vel_scale,
                          pose_connecting=FLAGS.connect_pose,
                          force_connecting=FLAGS.connect_force)
    node.run()


if __name__ == '__main__':
    app.run(main)    
