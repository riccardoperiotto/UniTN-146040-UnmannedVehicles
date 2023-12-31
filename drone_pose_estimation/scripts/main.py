#!/usr/bin/env python3

from std_msgs.msg import Float32MultiArray
from geometry_msgs.msg import PoseStamped
from rclpy.qos import qos_profile_system_default
from rclpy.node import Node
from Control.topics import *
from Control import Navigation
from Plot import class_name, Plot
import Algorithms
import rclpy
import time
import numpy as np
import os

from ament_index_python.packages import get_package_share_directory


PACKAGE_NAME = "drone_pose_estimation"

CHECK_UPDATE_TIME = 5.0
ANCHOR_MOV_TIME   = 1.0  # 1 s

SWARM_COEF = np.array([0.0, 1.0, 0.0])
ANCHOR_COEF = np.vstack([-np.eye(3), np.eye(3)])

SWARM_VEL = 0.2  # [m/s]
ANCHOR_VEL = 1.0

class Main(Node):

    def save_file(self):
        self.timer.cancel()

        # create folder for storing the data
        package_path = get_package_share_directory(PACKAGE_NAME)
        folder_path = os.path.join(
            package_path, self.data_folder, self.setting, self.run
        )
        os.makedirs(folder_path, exist_ok=True)

        with open(os.path.join(folder_path, 'X_storage.txt'), 'w') as file:
            np.savetxt(file, self.X_storage.reshape(
                self.X_storage.shape[0], -1))
            file.close()

        with open(os.path.join(folder_path, 'X_mds_storage.txt'), 'w') as file:
            np.savetxt(file, self.X_mds_storage.reshape(
                self.X_mds_storage.shape[0], -1))
            file.close()

        with open(os.path.join(folder_path, 'X_lsm_storage.txt'), 'w') as file:
            np.savetxt(file, self.X_lsm_storage.reshape(
                self.X_lsm_storage.shape[0], -1))
            file.close()

        with open(os.path.join(folder_path, 'times.txt'), 'w') as file:
            np.savetxt(file, self.times)
            file.close()


    def pose_reader_callback(self, received_msg, index):
        """
        Callback function for the POSE_TOPIC_TEMPLATE topic.
        Save the information sent over the topic in the coords data structure.
        It is activated only if 'environment' is set to 'test'
        """
        pos = received_msg.pose.position
        self.coords[:, index] = (Algorithms.M_ROT_TRASL_DRONE_GZ(
            index) @ np.array([pos.x, pos.y, pos.z, 1]))[:3]
        # self.get_logger().info(f"drone{index}: {str(self.coords[:, index])}")


    def distance_reader_callback(self, received_msg, index):
        """
        Update the Distance Matrix (DM) buffer by replacing the i-th columnn.
        """
        self.DM_buffer[:, index] = np.array(received_msg.data)

        if self.updating: self.update_booleans[index] = True


    def send_velocity(self, id, vel_x, vel_y, vel_z):
        # apply transformation to interact with Gazebo+Ardupilot
        M_GZ_DRONE = Algorithms.M_ROT_TRASL_GZ_DRONE(id-1)
        rel_vel = (M_GZ_DRONE @ np.array([vel_x, vel_y, vel_z, 0]))[:3]

        # send
        self.navigation.send_setpoint_velocity(
            id, rel_vel[0], rel_vel[1], rel_vel[2], 0.0
        )


    def initialize_swarm(self):
        """
        Initialize the swarm by appling the ArduCopter procedure.
            -1) Set mode to GUIDED
            -2) Arm the throttles
            -3) Take off to specified altitude
        """
        for id in range(1, self.n_drones+1):
            self.navigation.set_mode(id, "GUIDED")

        for id in range(1, self.n_drones+1):
            self.navigation.arm(id)
            self.navigation.takeoff(id, self.altitude)

        time.sleep(40.0)  # time to go up

        # set the z-coordinates of the anchor to "altitude", according to the takeoff
        self.PMs[2, 0] = self.altitude


    def MDS(self, DM, PMs_tmp):
        """
        Run MDS algorithm defined in the Algorithms class, by assembling the
        disance matrices in one unique [n+3, n+3] matrix.
        Return:
            - Coordinates of the drones swarm estimated via the algorithm.
        """
        t_mds = self.get_timestamp()

        # Run the algorithm
        X_mds = Algorithms.MDS(DM, PMs_tmp)

        # TODO: delete??
        # The following covariance doesn't represent the uncertainty on the estimation.
        # If storage is initialized with None it does not work; use zeros instead.
        # Compute the covaraince matrix
        # # # if (not self.iter_counter):
        # # #     Cov_mds = None
        # # # else:
        # # #     Cov_mds = np.zeros((9, self.n_drones))
        # # #     for i in range(self.n_drones):
        # # #         Cov_mds[:, i] = np.cov(
        # # #             self.X_mds_storage[:self.iter_counter+1, :, i], rowvar=False).flatten()

        return X_mds, None, self.get_timestamp()-t_mds


    def LSM(self, DM, PMs_tmp):
        """
        Run LSM algorithm defined in the Algorithms class, by assembling the
        disance matrices in one unique [n+3, n+3] matrix.
        Return:
            - Coordinates of the drones swarm estimated via the algorithm.
        """
        t_lsm = self.get_timestamp()

        # Run the algorithm
        X_lsm = Algorithms.LSM(DM, PMs_tmp)

        # TODO: delete??
        # The following covariance doesn't represent the uncertainty on the estimation.
        # If storage is initialized with None it does not work; use zeros instead.
        # Compute the covaraince matrix
        # # # if (not self.iter_counter):
        # # #     Cov_ls = None
        # # # else:
        # # #     Cov_ls = np.zeros((9, self.n_drones))
        # # #     for i in range(self.n_drones):
        # # #         Cov_ls[:, i] = np.cov(
        # # #             self.X_ls_storage[:self.iter_counter+1, :, i], rowvar=False).flatten()

        return X_lsm, None, self.get_timestamp()-t_lsm


    def update(self):
        """
        Update the following class parameters:
            - anchor position
            - distance matrix
            - run algorithms
            - update_booleans
            - indexes
            - movement time (new noise)
            - timestamp
        """
        # The algorithms require at least 4 iterations.
        # Avoid executing the algorithms if less than 4 iter.
        if (not self.algorithms and self.phase_index > 3):
            self.algorithms = True

        # Update anchor pos: x_(n) = x_(n-1) + Delta_x
        prev_pos = self.PMs[:, self.meas_index - 1]
        delta_s = ANCHOR_COEF[self.phase_index] * ANCHOR_VEL * ANCHOR_MOV_TIME
        self.PMs[:, self.meas_index] = prev_pos + delta_s

        # Store the just received distance matrix for the i-th (meas_index) iteration
        self.DMs[self.meas_index] = np.copy(self.DM_buffer)

        if (self.algorithms):
            # Shift based on the meas index
            DMs_tmp = np.roll(self.DMs, -self.meas_index, axis=0)
            PMs_tmp = np.roll(self.PMs, -self.meas_index, axis=1)

            # Assemble the full distance matrix
            DM = Algorithms.combine_matrices(
                DMs_tmp[0], DMs_tmp[1], DMs_tmp[2], DMs_tmp[3],
                PMs_tmp[:, 0], PMs_tmp[:, 1], PMs_tmp[:, 2], PMs_tmp[:, 3]
            )

            # Run algorithms
            X_mds, Cov_mds, time_mds = self.MDS(DM, PMs_tmp)
            X_lsm, Cov_lsm, time_lsm = self.LSM(DM, PMs_tmp)
            self.times[self.iter_counter] = np.array(
                [self.timestamp, time_mds, time_lsm])

            # Update the plot
            self.plot.update(
                true_coords=self.coords,
                MDS_coords=X_mds + self.offset.reshape(-1, 1),
                LSM_coords=X_lsm + self.offset.reshape(-1, 1),
                MDS_cov=Cov_mds,    # None now
                LSM_cov=Cov_lsm     # None now
            )

            # Store the values for plotting
            self.X_storage[self.iter_counter] = self.coords - \
                self.offset.reshape(3, -1)
            self.X_mds_storage[self.iter_counter] = X_mds
            self.X_lsm_storage[self.iter_counter] = X_lsm

            self.iter_counter += 1

        # Reset the booleans
        self.update_booleans[:] = False

        # Update cycle management 
        time_noise    = Algorithms.noise(0, self.noise_time_std, shape=1)
        self.mov_time = ANCHOR_MOV_TIME + time_noise

        self.meas_index  = (self.meas_index + 1) % self.n_meas
        self.phase_index = (self.phase_index + 1) % len(ANCHOR_COEF)

        self.timestamp = self.get_timestamp()


    def check_update(self):
        """
        Check whether the distances vectors for all the drones have been received. 
        In case, update the estimation and move the anchor.
        """
        if np.all(self.update_booleans):
            self.check_update_timer.cancel()
            self.update()
            self.updating = False


    def move_swarm(self, anchor):
        """
        Move the drone swarm by sending velcity values.
        The method affects the anchor motion if anchor set to True.
        """
        # Compute the velocity components
        vel_x, vel_y, vel_z = SWARM_COEF*SWARM_VEL

        # Send velocity value to all the drones and to the anchor, in case it's flagged
        for id in range(1, self.n_drones+1):
            if id != self.anchor_id or anchor:
                self.send_velocity(id, vel_x, vel_y, vel_z)


    def move_anchor(self):
        """
        Move the anchor dron by sending velcity values.
        The method does not affect the swarm motion.
        """
        # Compute the velocity components
        vel_x, vel_y, vel_z = SWARM_COEF * SWARM_VEL + \
            ANCHOR_COEF[self.phase_index] * ANCHOR_VEL

        # Send velocity value
        self.send_velocity(self.anchor_id, vel_x, vel_y, vel_z)


    def cycle_callback(self):
        """
        Node main loop. Update positions and perform measurments.
        """
        now_timestamp = self.get_timestamp()
        if self.updating:
            # move all the drones simultaneously
            self.move_swarm(anchor=True)
        else:
            if ((now_timestamp - self.timestamp) >= self.mov_time):
                # block the possibility to perform another update and tell the cb to flag the booleans
                self.updating = True

                # make the swarm to keep going on
                self.move_swarm(anchor=True)

                # leave the time to update the distances and perform the update
                self.check_update_timer = self.create_timer(
                    CHECK_UPDATE_TIME, self.check_update
                )
            else:
                # move the swarm normally and the anchor to the new position
                self.move_swarm(anchor=False)
                self.move_anchor()

        # update swarm position by integration
        self.offset += SWARM_COEF * SWARM_VEL * self.timestep

        if self.iter_counter == self.max_iteration:
            self.save_file()
            self.get_logger().info("End")
            exit(0)

    def start(self):
        self.timestamp = self.get_timestamp()

        self.move_swarm(anchor=True)

        self.timer = self.create_timer(self.timestep, self.cycle_callback)
        self.start_timer.cancel()


    def __init__(self):

        # Declare the ROS2 node
        class_name(self)
        super().__init__('main')
        print("Node that reads the distances, computes the coordinates, plots the results and guides the drones.")

        self.declare_parameter('altitude', rclpy.Parameter.Type.DOUBLE)
        self.altitude = self.get_parameter(
            'altitude').get_parameter_value().double_value

        # Parameters from ROS2 command line
        self.declare_parameter('environment', rclpy.Parameter.Type.STRING)
        self.environment = self.get_parameter(
            'environment').get_parameter_value().string_value

        self.declare_parameter('data_folder', rclpy.Parameter.Type.STRING)
        self.data_folder = self.get_parameter(
            'data_folder').get_parameter_value().string_value

        self.declare_parameter('max_iteration', rclpy.Parameter.Type.INTEGER)
        self.max_iteration = self.get_parameter(
            'max_iteration').get_parameter_value().integer_value

        self.declare_parameter('n_drones', rclpy.Parameter.Type.INTEGER)
        self.n_drones = self.get_parameter(
            'n_drones').get_parameter_value().integer_value

        self.declare_parameter('noise_time_std', rclpy.Parameter.Type.DOUBLE)
        self.noise_time_std = self.get_parameter(
            'noise_time_std').get_parameter_value().double_value

        self.declare_parameter('run', rclpy.Parameter.Type.STRING)
        self.run = self.get_parameter(
            'run').get_parameter_value().string_value

        self.declare_parameter('setting', rclpy.Parameter.Type.STRING)
        self.setting = self.get_parameter(
            'setting').get_parameter_value().string_value

        self.declare_parameter('timestep', rclpy.Parameter.Type.DOUBLE)
        self.timestep = self.get_parameter(
            'timestep').get_parameter_value().double_value

        self.declare_parameter('seed', rclpy.Parameter.Type.INTEGER)
        self.seed = self.get_parameter(
            'seed').get_parameter_value().integer_value

        np.random.seed(self.seed)

        # Attributes initialization
        # management
        self.phase_index, self.meas_index = 0, 0
        self.anchor_id = 1  # only works if anchor_id = 1 atm
        self.n_meas = 4
        self.mov_time = ANCHOR_MOV_TIME
        self.algorithms = False
        self.updating = False
        self.update_booleans = np.zeros((self.n_drones,), dtype=bool)
        self.iter_counter = 0

        # measurements and storage
        self.coords = np.zeros((3, self.n_drones))
        self.offset = np.zeros((3,))

        self.PMs       = np.zeros((3, self.n_meas))
        self.DMs       = np.zeros((self.n_meas, self.n_drones, self.n_drones))
        self.DM_buffer = np.zeros((self.n_drones, self.n_drones))

        self.X_storage     = np.zeros((self.max_iteration, 3, self.n_drones))
        self.X_mds_storage = np.zeros((self.max_iteration, 3, self.n_drones))
        self.X_lsm_storage = np.zeros((self.max_iteration, 3, self.n_drones))

        self.times = np.zeros((self.max_iteration, 3))

        # Subscribe to DISTANCE_TOPIC_TEMPLATE topic for each drone
        for i in range(self.n_drones):
            self.get_logger().info(f"Read from {DISTANCE_TOPIC_TEMPLATE(i+1)}")
            self.create_subscription(
                Float32MultiArray,
                DISTANCE_TOPIC_TEMPLATE(i+1),
                lambda msg, i=i: self.distance_reader_callback(msg, i),
                qos_profile_system_default
            )

        # Subscribe to POSE_TOPIC_TEMPLATE topic for each drone
        for i in range(self.n_drones):
            self.get_logger().info(f"Read from {POSE_TOPIC_TEMPLATE(i+1)}")
            self.create_subscription(
                PoseStamped,
                POSE_TOPIC_TEMPLATE(i+1),
                lambda msg, i=i: self.pose_reader_callback(msg, i),
                qos_profile_system_default
            )

        # Initialize Navigation object
        self.navigation = Navigation(node=self, n_drones=self.n_drones, timeout=10
                                     )
        if (self.environment == "gazebo"): 
            print("Initializing takeoff procedure...")
            self.initialize_swarm()

            # Plotting
            self.plot = Plot(   # do not display plot -> it slows down the performances
                mode='2D',
                display_MDS=False,
                display_LSM=False,
                reduction_method='xy',
                display_covariance=False,
            )
        else:
            # Plotting
            self.plot = Plot(
                mode='2D',
                display_MDS=True,
                display_LSM=True,
                reduction_method='xy',
                display_covariance=True,
            )

        self.plot.start()

        # Time management
        def get_timestamp():
            now = self.get_clock().now().to_msg()
            return now.sec+now.nanosec/1e9

        self.get_timestamp = get_timestamp
        self.timestamp = 0.0     # to stop the anchor movement

        # Just wait some seconds (10) and start..
        # Needed to allow a better estimation of the anchor movement
        self.start_timer = self.create_timer(10, self.start)


def main(args=None):
    rclpy.init(args=args)
    main = Main()
    rclpy.spin(main)

    main.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
