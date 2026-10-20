"""店舗注文システム一式を起動する

  ros2 launch shop_robot shop_system.launch.py
  ros2 launch shop_robot shop_system.launch.py use_handover:=true use_mock_robot:=true   # 受取ノード + 偽ロボット
  ros2 launch shop_robot shop_system.launch.py use_handover:=true                        # 受取ノード + 実機
  ros2 launch shop_robot shop_system.launch.py use_stub:=false        # 実機の動作ノードを別に起動する場合
  ros2 launch shop_robot shop_system.launch.py params_file:=/path/to/my_params.yaml
"""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration, PythonExpression
from launch_ros.actions import Node


def generate_launch_description():
    default_params = os.path.join(get_package_share_directory("shop_robot"), "config", "params.yaml")
    params = LaunchConfiguration("params_file")
    use_stub = LaunchConfiguration("use_stub")
    use_handover = LaunchConfiguration("use_handover")

    return LaunchDescription([
        DeclareLaunchArgument("params_file", default_value=default_params),
        DeclareLaunchArgument("use_stub", default_value="true",
                              description="準備動作のスタブを起動する（use_handover:=true のときは起動しない）"),
        DeclareLaunchArgument("use_handover", default_value="false", description="受取ノード（handover）を起動する"),
        DeclareLaunchArgument("use_mock_robot", default_value="false", description="受取ノード用の偽ロボットを起動する"),

        # 店舗システム本体（Web サーバ・待ち行列・AR マーカー読み取り）。ロボットとは /shop/prepare_order と /shop/prepare_done だけ
        Node(package="shop_robot", executable="shop_server", name="shop_server",
             parameters=[params], output="screen"),
        Node(package="shop_robot", executable="prepare_stub", name="prepare_stub",
             parameters=[params], output="screen",
             condition=IfCondition(PythonExpression(["'", use_stub, "' == 'true' and '", use_handover, "' != 'true'"]))),
        Node(package="shop_robot", executable="handover", name="handover",
             parameters=[params], output="screen", condition=IfCondition(use_handover)),
        Node(package="shop_robot", executable="mock_robot", name="mock_robot",
             parameters=[params], output="screen", condition=IfCondition(LaunchConfiguration("use_mock_robot"))),
    ])
