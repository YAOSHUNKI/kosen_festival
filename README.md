# 店舗注文システム（ROS 2 Humble）

注文を Web 画面で登録 → ROS Topic でロボット側へ送信 → スタッフが商品を準備して「完成」を押す →
呼び出し表示に番号が出る → お客様が番号札の ArUco マーカーをカメラにかざすとロボットが動作、という流れを実装したワークスペース。

```
受付 ─▶ 準備中 ──[完成ボタン]──▶ 呼び出し中（/display に表示）──[その番号のマーカー]──▶ ロボット動作 ──▶ お渡し完了
          ▲  └──[準備中に戻す]──┘           ▲                                            │
          │                                 └──────────── 失敗 / 開始できない ───────────┘
```

```
 店頭タブレット/スマホ (ブラウザ)               ロボット側
 ┌───────────────────────┐   /shop/orders (Order)    ┌────────────────┐  /shop/prepare_order   ┌──────────────────┐
 │ order_web_server       │ ───────────────────────▶ │ order_queue     │ ─────(Action)────────▶ │ prepare_stub      │
 │  注文・商品管理・待ち行列 │ ◀─────────────────────── │  待ち行列+状態機械 │ ◀──── feedback/result │ （実機動作に置換） │
 │  /display 呼び出し表示   │  /shop/queue_status       └────────────────┘                        └──────────────────┘
 └───────────────────────┘  /shop/queue_control(srv)          ▲ /shop/marker_id (Int32)
                                                      ┌────────┴────────┐
                                                      │ aruco_detector   │ ← USBカメラ or 画像トピック
                                                      └─────────────────┘
```

## ROS インタフェース

| 名前 | 型 | 向き | 内容 |
|---|---|---|---|
| `/shop/orders` | `shop_interfaces/Order` | Web → キュー | 注文番号・商品ID・個数（`OrderItem[]`）。RELIABLE / VOLATILE |
| `/shop/queue_status` | `shop_interfaces/QueueStatus` | キュー → Web | 状態・準備中(`pending`)・呼び出し中(`ready`)・ロボット動作中(`active`)・完了番号・お客様向けメッセージ。TRANSIENT_LOCAL、変化時＋1Hz |
| `/shop/marker_id` | `std_msgs/Int32` | 検出 → キュー | 確定したマーカーID（= 注文番号） |
| `/shop/queue_control` | `shop_interfaces/QueueControl` (srv) | Web → キュー | 取消 / 完成（呼び出し） / 準備中に戻す / マーカーなしで開始 / マーカー読取の再現 |
| `/shop/prepare_order` | `shop_interfaces/PrepareOrder` (action) | キュー → 動作 | マーカー提示後のロボット動作（1注文）。feedback で進捗、cancel 対応 |

キューの動き：スタッフが「完成」を押した注文だけが呼び出し中になる（完成の順番は自由）。
呼び出し中の番号のマーカーならどれでも受け付け、ロボット動作中は次のマーカーを受け付けない（「前のお客様の対応中です」と表示）。
準備中の番号のマーカーには「まだ準備中です」と表示する。ロボット動作が失敗したら注文は呼び出し中に戻る
（スタッフが「マーカーなしで開始」で再開できる）。状態は `~/.shop_robot/queue_state.json` に保存され、ノードを再起動しても復元される。

## セットアップ（Ubuntu 22.04 + ROS 2 Humble）

```bash
sudo apt install ros-humble-cv-bridge python3-opencv python3-pil
pip3 install "fastapi>=0.100" "uvicorn>=0.23"

cd kosensai_shop_ws
colcon build --symlink-install
source install/setup.bash
```

## 起動

```bash
ros2 launch shop_robot shop_system.launch.py
```

- スタッフ画面: `http://<PCのIP>:8000/`（同じ Wi-Fi のタブレット・スマホから）
- 呼び出し表示: `http://<PCのIP>:8000/display`（お客様向けモニタで全画面表示）

| 引数 | 既定 | 用途 |
|---|---|---|
| `use_stub` | true | false にすると準備動作スタブを起動しない（実機ノードを別途起動） |
| `use_camera` | true | false でカメラなし。画面の「動作確認」でマーカー読取を再現できる |
| `use_web` | true | Web サーバを別PCで動かす場合は false |
| `params_file` | config/params.yaml | カメラ番号・辞書・ポートなど |

Web サーバとロボット側を別PCで動かす場合は、両方で同じ `ROS_DOMAIN_ID` を設定し、同じネットワークに置く。

### ROS なしで画面だけ試す

```bash
cd src/shop_robot
pip3 install fastapi uvicorn opencv-contrib-python pillow
python3 -m shop_robot.demo          # → http://localhost:8000/
```
キューと準備動作を同じプロセス内で模擬する。サンプル商品は `./demo_data/products.json` に入る。

## 番号札（ArUco マーカー）の印刷

```bash
ros2 run shop_robot make_marker_cards --start 1 --end 60 --shop-name "○○屋台" -o cards.pdf
```

A4 に 12 枚（マーカー 40mm）。**等倍（100%）で印刷**する。`--dict` は `params.yaml` の `aruco_dictionary`
と必ず揃える（既定 `DICT_4X4_1000` → 番号 0〜999）。マーカーの周りの白い余白は認識に必要なので切り落とさない。
1 枚だけ欲しいときは `http://<PCのIP>:8000/api/marker/<番号>.png` で画像を出せる。

## 運用の流れ

1. 「商品」タブで商品を登録（商品IDはロボット側がどの商品を準備するかの識別に使う）
2. 注文を受けたら番号札を渡し、「注文」タブで商品をタップ → 番号札の番号を入力 → 送信
   （番号欄には次の候補が自動で入る。送信後「ロボットに送信しました ✓」が出ればキューに入っている）
3. 「待ち行列」タブの「準備中」に注文内容が並ぶ。商品ができたら「完成」を押す
   → 呼び出し表示に番号が出る（新しい番号は点滅＋呼び出し音。音は表示画面を一度クリックすると有効になる）
4. お客様が札をカメラにかざす → ロボットが動作開始
5. 押し間違えたら「準備中に戻す」、札をなくしたときは「マーカーなしで開始」

## 実機の動作に置き換える

`shop_robot/nodes/prepare_stub_node.py` の `PrepareServerBase` を継承し、`prepare_unit()` に
「商品を1個用意する」動作を書いた新しいノードを作る。キュー・Web 側は変更不要。

```python
class MyRobotServer(PrepareServerBase):
    def __init__(self):
        super().__init__("prepare_robot")

    def prepare_unit(self, item, k, goal_handle) -> bool:
        # item.product_id / item.quantity / k（何個目か）
        if goal_handle.is_cancel_requested:
            return True
        ...  # アームを動かす
        return True  # 失敗なら False
```

起動時は `use_stub:=false` にして自作ノードを別に起動する。ActionServer は MultiThreadedExecutor で
spin すること（キャンセル要求を動作中に受け取るため）。

## テスト

```bash
cd src/shop_robot && python3 -m pytest test/
```
コアロジック（キュー状態機械・商品管理・ArUco 検出）と Web API を ROS なしでテストする。

## 動作確認用コマンド

```bash
ros2 topic echo /shop/orders
ros2 topic echo /shop/queue_status --qos-durability transient_local
ros2 topic pub --once /shop/marker_id std_msgs/msg/Int32 "{data: 12}"   # 12番のマーカーを見せたことにする
ros2 run rqt_image_view rqt_image_view /shop/aruco/debug_image          # publish_debug_image:=true のとき
```

## データ

| ファイル | 内容 |
|---|---|
| `~/.shop_robot/products.json` | 商品マスタ |
| `~/.shop_robot/queue_state.json` | 待ち行列（営業日を切り替えるときはノード停止中に削除） |

## 補足

- Web 画面は外部 CDN・フォントを使わないので、会場にインターネットがなくても動く
- OpenCV は 4.5 系（Ubuntu 22.04 の apt 版）と 4.7 以降の ArUco API の両方に対応
- 注文の送信自体は Topic（返答なし）なので、Web サーバは送信後に `/shop/queue_status` に注文が現れるのを
  最大 2 秒待って受付を確認している。確認できないときは画面に警告が出る
