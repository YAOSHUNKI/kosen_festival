import pytest

from shop_robot.core import codec
from shop_robot.core.models import Order, OrderItem


def test_items_roundtrip():
    o = Order(12, [OrderItem(1, "たこ焼き", 2), OrderItem(3, "ラムネ", 1)])
    data = codec.encode_items(o)
    assert data == [1, 2, 3, 1]               # 注文番号は含まない
    items = codec.decode_items(data, {1: "たこ焼き"})
    assert [(i.product_id, i.product_name, i.quantity) for i in items] == [(1, "たこ焼き", 2), (3, "", 1)]


@pytest.mark.parametrize("bad", [[], [1], [1, 2, 3], [1, 0]])
def test_items_invalid(bad):
    with pytest.raises(codec.CodecError):
        codec.decode_items(bad)
