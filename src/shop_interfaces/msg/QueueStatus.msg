# 注文キューの状態（/shop/queue_status, transient_local で最新値を保持）
#
#  受付 ─▶ pending（スタッフが準備中）─[完成ボタン]─▶ ready（呼び出し中）─[マーカー]─▶ active（ロボット動作）─▶ 完了
#
uint8 STATE_IDLE=0          # 注文なし
uint8 STATE_PENDING=1       # 準備中の注文のみ（呼び出し中なし）
uint8 STATE_CALLING=2       # 呼び出し中の注文あり、マーカー提示待ち
uint8 STATE_ROBOT_BUSY=3    # ロボット動作中

builtin_interfaces/Time stamp
uint8 state

bool has_active                # ロボット動作中の注文があるか
Order active                   # ロボット動作中の注文（has_active=false のときは無効）
float32 progress               # ロボット動作の進捗 0.0-1.0
string progress_text           # ロボット動作の現在ステップ

Order[] pending                # スタッフが準備中（受付順）
Order[] ready                  # 準備完了・呼び出し中（完成順）
uint32[] recently_completed    # 最近お渡しが完了した注文番号（新しい順）

string last_event              # 直近のイベント（スタッフ画面用）
string public_message          # お客様向けメッセージ（呼び出し表示用）
uint32 public_seq              # public_message を出すたびに増える（同じ文面の再表示判定用）
