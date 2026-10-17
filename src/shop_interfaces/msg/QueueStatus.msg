# 注文キューの状態（/shop/queue_status, transient_local で最新値を保持）
uint8 STATE_IDLE=0             # 待ち注文なし
uint8 STATE_WAITING_MARKER=1   # 先頭の注文のマーカー提示待ち
uint8 STATE_PREPARING=2        # ロボットが商品準備中

builtin_interfaces/Time stamp
uint8 state

bool has_active                # 準備中の注文があるか
Order active                   # 準備中の注文（has_active=false のときは無効）
float32 progress               # 準備の進捗 0.0-1.0
string progress_text           # 準備の現在ステップ

Order[] waiting                # 待ち行列（先頭 = 次に呼び出す注文）
uint32[] recently_completed    # 最近完了した注文番号（新しい順）
string last_event              # 直近のイベント（画面表示用）
