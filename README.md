# 店舗注文システム（ROS 2 Humble）

注文を Web 画面で登録 → ROS Topic でロボット側へ送信 → スタッフが商品を準備して「完成」を押す →
呼び出し表示に番号が出る → お客様が番号札の ArUco マーカーをカメラにかざすとロボットが動作、という流れを実装したワークスペース。

```
受付 ─▶ 準備中 ──[完成ボタン]──▶ 呼び出し中（/display に表示）──[その番号のマーカー]──▶ ロボット動作 ──▶ お渡し完了
          ▲  └──[準備中に戻す]──┘           ▲                                            │
          │                                 └──────────── 失敗 / 開始できない ───────────┘
```

```
 店頭タブレット/スマホ (ブラウザ)
        │ HTTP
 ┌──────▼────────────────────────────────┐  /shop/prepare_order (Int32MultiArray)  ┌──────────────────┐
 │ shop_server（1ノード）                  │ ──────────────────────────────────────▶ │ handover（受取）   │
 │  注文・商品管理の Web 画面 / 呼び出し表示  │   [商品ID, 個数, 商品ID, 個数, ...]        │ or prepare_stub   │
 │  待ち行列 / AR マーカー読み取り / カメラ映像 │                                          │ or 自作ノード      │
 │                                        │ ◀────────────────────────────────────── │ （ROS1・C++ でも可）│
 └──────▲────────────────────────────────┘  /shop/prepare_done (Bool)              └──────────────────┘
        │ USB カメラ（または画像トピック）
```

## ROS インタフェース（ロボットとのやり取りはこの2本だけ・標準メッセージ型）

| トピック | 型 | 向き | 内容 |
|---|---|---|---|
| `/shop/prepare_order` | `std_msgs/Int32MultiArray` | システム → ロボット | `data = [商品ID, 個数, 商品ID, 個数, ...]`。**呼び出し中の番号札（AR マーカー）が読まれたときだけ**、その注文の分を1回 publish する。登録されている商品はすべて入れ、注文されていない商品は個数 0（IDの小さい順で固定） |
| `/shop/prepare_done` | `std_msgs/Bool` | ロボット → システム | 終わったら `true`（成功）/ `false`（失敗）。届くまで次の注文は送らない |

- 注文番号はトピックに含まない（ロボットは商品IDと個数だけを見ればよい）。RELIABLE / VOLATILE
- 送る商品IDは、商品マスタの「ロボット用ID」（空欄なら商品ID）。割引版など中身が同じ商品は元の商品のIDにしておくと、
  ロボットには同じIDで届き、個数は合計される（例: たこ焼き ID 1 ×2 ＋ たこ焼き割引 ID 11・ロボット用ID 1 ×1 → `[1, 3]`）。
  送るIDは受付時点の設定で決まる（受付後に商品設定を変えても、受付済みの注文には影響しない）
- 例: 登録が ID 1・2・3・4 と 11（ロボット用ID 1）で、注文が ID 3 ×2 と ID 11 ×1 → `[1, 1, 2, 0, 3, 2, 4, 0]`。
  ロボット側は個数 0 の商品は作らない（`PrepareNodeBase` は自動で飛ばす）
- Web 画面・待ち行列・マーカー読み取りは `shop_server` の中で動くので、それらの間に ROS トピックはない
- 形式の定義と変換関数は `shop_robot/core/codec.py`。std_msgs だけなので ros1_bridge などでもそのまま中継できる

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
- 呼び出し表示: `http://<PCのIP>:8000/display`（お客様向けモニタで全画面表示。カメラ映像を出さないなら `/display?camera=0`）

### カメラ映像

shop_server がカメラ画像に検出結果を重ねて、スタッフ画面（待ち行列タブの「カメラ」）と
呼び出し表示の右上に映す。マーカーの枠の色は **緑 = 呼び出し中（かざせば受け付ける） / 橙 = 準備中・動作中 /
赤 = 受付中にない番号**。映像だけ見たいときは `http://<PCのIP>:8000/api/camera.mjpg`、静止画は `/api/camera.jpg`。
映像は既定で鏡像（`preview_mirror`）。検出には影響しない。

呼び出し表示（`/display`）は、左にキャラクターとカメラ映像、右にお呼び出し中の番号を1行に1件ずつ並べる。
番号は1件ずつ枠で囲んで「番」を付けるので、「1」「2」と「12」を取り違えない（5件以上で小さく、7件以上で2列、13件以上は番号だけの4列）。
キャラクター画像は `shop_robot/web/static/char.png`。差し替えるときは背景色を `#B94047` にすると左パネルと継ぎ目なくつながる。

| 引数 | 既定 | 用途 |
|---|---|---|
| `use_stub` | true | false にすると準備動作スタブを起動しない（実機ノードを別途起動） |
| `use_handover` | false | true で受取ノード（受渡し台 → 手渡し）を起動。スタブは起動しない |
| `use_mock_robot` | false | true で受取ノード用の偽ロボットを起動（実機なしで受取の流れを確認） |
| `params_file` | config/params.yaml | カメラ番号・辞書・ポートなど |

カメラが開けないときも shop_server は起動を続ける（画面の「動作確認」でマーカー読み取りを再現できる）。
ロボットのカメラを使う場合は `camera_device: -1` と `image_topic` を設定する。
shop_server とロボット側を別PCで動かす場合は、両方で同じ `ROS_DOMAIN_ID` を設定し、同じネットワークに置く。

### ROS なしで画面だけ試す

```bash
cd src/shop_robot
pip3 install fastapi uvicorn opencv-contrib-python pillow
python3 -m shop_robot.demo          # → http://localhost:8000/
```
ロボット動作を同じプロセス内で模擬する。サンプル商品は `./demo_data/products.json` に入る。
`python3 -m shop_robot.demo --camera 0` で PC の Web カメラを使うと、番号札をかざしての読み取りとカメラ映像も試せる。

## 番号札（ArUco マーカー）の印刷

```bash
ros2 run shop_robot make_marker_cards --start 1 --end 60 --shop-name "○○屋台" -o cards.pdf
```

A4 に 12 枚（マーカー 40mm）。**等倍（100%）で印刷**する。`--dict` は `params.yaml` の `aruco_dictionary`
と必ず揃える（既定 `DICT_4X4_1000` → 番号 0〜999）。マーカーの周りの白い余白は認識に必要なので切り落とさない。
1 枚だけ欲しいときは `http://<PCのIP>:8000/api/marker/<番号>.png` で画像を出せる。

## 運用の流れ

1. 「商品」タブで商品を登録（ロボットには「ロボット用ID」を送る。空欄なら商品ID。割引版は元の商品のIDを入れる）
2. 注文を受けたら番号札を渡し、「注文」タブで商品をタップ → 番号札の番号を入力 → 送信
   （番号欄には次の候補が自動で入る。この時点ではロボットには何も送らない）
3. 「待ち行列」タブの「準備中」に注文内容が並ぶ。商品ができたら「完成」を押す
   → 呼び出し表示に番号が出る（新しい番号は点滅＋呼び出し音。音は表示画面を一度クリックすると有効になる）
4. お客様が札をカメラにかざす → `/shop/prepare_order` に `[商品ID, 個数, ...]` を送信 → ロボットが動作開始
   → ロボットが `/shop/prepare_done` を返したら「お渡し完了」
5. 押し間違えたら「準備中に戻す」、札をなくしたときは「マーカーなしで開始」。
   ロボットが完了を返さないときは「取り消し扱いにする」（ロボット自体は止まらない。後から届いた完了は無視する）
6. 番号札を一巡したら「待ち行列」タブの「番号札のリセット」で 1番（または指定の番号）から振り直す。
   営業開始・終了時は「全注文をクリア」で注文・完了履歴を消して 1番に戻す

## 受取ノード（ロボット側）

`handover` ノードが `/shop/prepare_order` を受け取り、マーカーが読まれた注文について
**受渡し台の商品をつかむ → お客様に差し出す → 受け取られたら離す → 初期姿勢** を実行する。

```bash
ros2 launch shop_robot shop_system.launch.py use_handover:=true use_mock_robot:=true   # 偽ロボットで流れを確認
ros2 launch shop_robot shop_system.launch.py use_handover:=true                        # 実機
```

動作の中身は `config/handover.yaml` に「トピックごとの動作オブジェクト（motion）」として書く。
コードを変えずに、実機のコントローラが受けるトピック名・型・値に合わせて書き換えればよい。

```yaml
topics:                     # 使うトピック（型は何でもよい。JointTrajectory なども可）
  arm_cmd:   {topic: /robot/arm/pose_name, type: std_msgs/msg/String}
  arm_state: {topic: /robot/arm/arrived,   type: std_msgs/msg/String}
motions:                    # 動作 = 指令を出して(pub/msg)、完了を待つ(done/match)
  to_counter:
    label: 受渡し台へ        # ログと /shop/handover/state に出る
    pub: arm_cmd
    msg: {data: counter}
    done: arm_state
    match: {data: counter}  # {wrench.force.z: {lt: 2.0}} のような比較も可
    timeout: 10.0
sequence: [to_counter, grasp, to_present, call, wait_taken, release, thanks]
after: [home]
on_abort: [to_counter, release, home]   # 失敗・取消時の後始末（差し出した商品を台に戻す）
estop: {topic: estop, match: {data: true}}
```

- `repeat: order`（既定）は 1 注文につき 1 回渡す。`item` / `unit` にすると商品ごと / 1 個ごとに sequence を繰り返し、
  msg の中で `{product_id}` などが使える（注文番号はロボットに届かないので `{order_number}` は使わない）
- 失敗（つかめない・受け取られない・タイムアウト）→ 後始末 → 注文は呼び出し中に戻る（スタッフが「マーカーなしで開始」で再開できる）
- 非常停止（`estop` の条件が成立）→ その場で中断し、後始末もしない。成立中は新しい注文を受け付けない
- お客様が受け取ったことの検出（`taken`）は、グリッパの電流・力覚センサの変化などをロボット側で Bool にして出す想定。
  センサが無ければ `wait_taken` を `duration: 5.0`（一定時間差し出してから離す）にする
- 起動時に YAML の書き間違い（存在しないトピック名・動作名・プレースホルダ・msg のフィールド名）を検出して止まる
- 実行中の動作は `/shop/handover/state`（JSON）に出る: `ros2 topic echo /shop/handover/state`

偽ロボット（`mock_robot`）は同じ YAML を読み、各動作の done トピックに match を満たすメッセージを返す。
`-p fail_motion:=grasp` でつかみ失敗、`ros2 topic pub --once /robot/estop std_msgs/msg/Bool "{data: true}"` で非常停止を試せる。

## 実機の動作に置き換える（自作ノード）

受取ノード（handover）を使わずにロボット側を自作する場合は、次の2つだけ守ればよい（言語・ROS1/ROS2 は問わない）。

1. `/shop/prepare_order`（Int32MultiArray `[商品ID, 個数, 商品ID, 個数, ...]`）を受け取ったら動作を始める（個数 0 の商品は作らない）
2. 終わったら `/shop/prepare_done`（Bool）に `true`（成功）/ `false`（失敗）を送る。受け付けられないときもすぐ `false` を返す

ROS2 の Python なら `shop_robot/nodes/prepare_stub_node.py` の `PrepareNodeBase` を継承すると 1・2 は自動で行われる。
1個ずつ用意するなら `prepare_unit()`、依頼全体をまとめて扱うなら `execute_order()` を書く（受取ノードは後者の例）。

```python
from shop_robot.nodes.prepare_stub_node import PrepareNodeBase

class MyRobot(PrepareNodeBase):
    def __init__(self):
        super().__init__("prepare_robot")

    def prepare_unit(self, item, k) -> bool:
        # item.product_id / item.quantity / k（何個目か）
        ...  # アームを動かす
        return True  # 失敗なら False（注文は呼び出し中に戻る）
```

起動時は `use_stub:=false` にして自作ノードを別に起動する。
`/shop/prepare_order` の購読者がいないときは、shop_server は依頼を送らず画面に「ロボット側が起動していません」と出す。

## テスト

```bash
cd src/shop_robot && python3 -m pytest test/
```
コアロジック（キュー状態機械・商品管理・ArUco 検出・受取動作の実行・トピック形式の変換）と Web API を ROS なしでテストする。

## 動作確認用コマンド

```bash
ros2 topic echo /shop/prepare_order                                              # AR を読んだときに届く配列
ros2 topic pub --once /shop/prepare_done std_msgs/msg/Bool "{data: true}"         # ロボットの完了を手で返す（use_stub:=false のとき）
ros2 run shop_robot prepare_stub                                                 # スタブだけ別に起動
```
ロボットなしで試すときは、画面の「待ち行列」→「動作確認」でマーカー読み取りを再現できる。

## データ

| ファイル | 内容 |
|---|---|
| `~/.shop_robot/products.json` | 商品マスタ |
| `~/.shop_robot/numbering.json` | 次の番号札の候補（再起動しても続きから振る） |
| `~/.shop_robot/queue_state.json` | 注文の状態（画面の「全注文をクリア」で空にできる） |

## 補足

- Web 画面は外部 CDN・フォントを使わないので、会場にインターネットがなくても動く
- OpenCV は 4.5 系（Ubuntu 22.04 の apt 版）と 4.7 以降の ArUco API の両方に対応
