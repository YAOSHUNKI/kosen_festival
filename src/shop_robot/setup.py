from glob import glob

from setuptools import find_packages, setup

package_name = "shop_robot"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    package_data={package_name: ["web/static/*"]},
    include_package_data=True,
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        ("share/" + package_name + "/launch", glob("launch/*.launch.py")),
        ("share/" + package_name + "/config", glob("config/*.yaml")),
    ],
    install_requires=["setuptools"],
    zip_safe=False,
    maintainer="shunki",
    maintainer_email="riml2005@icloud.com",
    description="注文受付 Web アプリ・注文キュー・ArUco 呼び出し・準備アクション",
    license="MIT",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "order_web_server = shop_robot.nodes.web_server_node:main",
            "order_queue = shop_robot.nodes.order_queue_node:main",
            "aruco_detector = shop_robot.nodes.aruco_detector_node:main",
            "prepare_stub = shop_robot.nodes.prepare_stub_node:main",
            "make_marker_cards = shop_robot.tools.make_marker_cards:main",
            "shop_demo = shop_robot.demo:main",
        ],
    },
)
