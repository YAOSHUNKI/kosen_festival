import pytest

from shop_robot.core import codec
from shop_robot.core.models import Order, OrderItem


def test_order_roundtrip():
    o = Order(12, [OrderItem(1, "たこ焼き", 2), OrderItem(3, "ラムネ", 1)])
    data = codec.encode_order(o)
    assert data == [12, 1, 2, 3, 1]
    back = codec.decode_order(data, {1: "たこ焼き"})
    assert back.order_number == 12
    assert [(i.product_id, i.product_name, i.quantity) for i in back.items] == [(1, "たこ焼き", 2), (3, "", 1)]


@pytest.mark.parametrize("bad", [[], [1], [1, 2], [1, 2, 3, 4], [-1, 1, 1]])
def test_order_invalid(bad):
    with pytest.raises(codec.CodecError):
        codec.decode_order(bad)


def test_command_and_result():
    assert codec.decode_command(codec.encode_command(17, "ready", 12)) == (17, "ready", 12)
    with pytest.raises(codec.CodecError):
        codec.decode_command("1 explode 3")
    assert codec.decode_command_result(codec.encode_command_result(17, False, "12番 は 準備中にありません")) == \
        (17, False, "12番 は 準備中にありません")
    assert codec.decode_command_result("5 ok") == (5, True, "")
    assert codec.decode_result([12, codec.RESULT_SUCCESS]) == (12, 1)
    assert codec.decode_progress([12.0, 0.5]) == (12, 0.5)
