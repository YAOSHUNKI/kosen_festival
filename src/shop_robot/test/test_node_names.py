"""ノードのクラスが rclpy.node.Node の属性名を上書きしていないか確認する（ROS なしで実行できる）。

例: メソッドに handle という名前を付けると Node.handle を潰し、起動時に
    AttributeError: __enter__ で落ちる。
"""
import ast
from pathlib import Path

# rclpy (Humble) の Node が持つ属性・メソッド名（意図して上書きしてよいもの: destroy_node）
RCLPY_NODE_NAMES = set("""
publishers subscriptions clients services timers guards waitables executor context default_callback_group handle
get_name get_namespace get_clock get_logger declare_parameter declare_parameters undeclare_parameter has_parameter
get_parameter_types get_parameter get_parameter_or get_parameters get_parameters_by_prefix set_parameters
set_parameters_atomically add_on_set_parameters_callback remove_on_set_parameters_callback describe_parameter
describe_parameters set_descriptor add_waitable remove_waitable resolve_topic_name resolve_service_name
create_publisher create_subscription create_client create_service create_timer create_guard_condition create_rate
destroy_publisher destroy_subscription destroy_client destroy_service destroy_timer destroy_guard_condition
destroy_rate get_topic_names_and_types get_service_names_and_types get_node_names get_fully_qualified_name
count_publishers count_subscribers count_clients count_services get_publishers_info_by_topic
get_subscriptions_info_by_topic wait_for_node
_parameters _context _logger _clock _executor _descriptors _rate_group _time_source _parameter_overrides
_allow_undeclared_parameters _default_callback_group _parameters_callbacks _parameter_event_publisher
_parameter_service _type_description_service
""".split())
ALLOWED = {"destroy_node"}

NODES_DIR = Path(__file__).resolve().parents[1] / "shop_robot" / "nodes"


def test_no_rclpy_name_collisions():
    hits = []
    for f in sorted(NODES_DIR.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            names = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            names |= {n.attr for n in ast.walk(cls)
                      if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)
                      and isinstance(n.value, ast.Name) and n.value.id == "self"}
            hits += [f"{f.name}:{cls.name}.{x}" for x in sorted((names & RCLPY_NODE_NAMES) - ALLOWED)]
    assert not hits, f"rclpy.Node の属性を上書きしています: {hits}"
