"""离线验证发送目标通道路由（不启动浏览器、台账写临时目录）。

覆盖用户反馈的 bug：后续手动新增的好友，勾选保存后不会被发送。
根因：手动新增条目 has_conversation=False，被误判"无会话"分流到通道 B，
而通道 B 受「允许首条消息」开关约束，默认关闭 → 静默 skipped 不发送。

修复后：channel="none"（手动添加/旧配置迁移）一律按正常私信（通道 A）发送。

运行：python3 tests/test_target_routing.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import ledger
from core.automation import _route_send_channel, compute_pending, run_send

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    print(("PASS" if cond else "FAIL"), name)
    PASS += cond
    FAIL += (not cond)


# ── 环境隔离：台账写到临时目录 ───────────────────────────────
tmp = Path(tempfile.mkdtemp(prefix="routing_test_"))
orig_account_dir = ledger.account_dir
ledger.account_dir = lambda account_id=None: tmp


def seed_ledger(entries):
    ledger._save(entries)


def entry(name, *, selected=True, has_conversation=False, channel="none"):
    return {
        **ledger._default_entry(name),
        "display_name": name,
        "selected": selected,
        "has_conversation": has_conversation,
        "channel": channel,
    }


# ── 1. 纯函数：_route_send_channel 路由矩阵 ──────────────────
check("T1 有会话 → 通道A", _route_send_channel(entry("a", has_conversation=True)) == "consumer")
check("T2 creator来源无会话 → 通道B", _route_send_channel(entry("a", channel="creator")) == "creator")
check("T3 手动添加(none)无会话 → 通道A【核心修复】",
      _route_send_channel(entry("a", channel="none")) == "consumer")
check("T4 曾同步(consumer)无会话 → 通道A",
      _route_send_channel(entry("a", channel="consumer")) == "consumer")
check("T5 有会话的creator → 通道A", _route_send_channel(entry("a", has_conversation=True, channel="creator")) == "consumer")

# ── 2. set_selected 模拟网页保存：手动新增好友的落库形态 ─────
seed_ledger([])
stats = ledger.set_selected([{"display_name": "新好友甲", "selected": True, "selected_order": 1}])
saved = {e["display_name"]: e for e in ledger.load_ledger()}
check("T6 手动新增落库 selected=True", stats["added"] == 1 and saved["新好友甲"]["selected"] is True)
check("T7 手动新增 has_conversation=False（未同步前如实记录）",
      saved["新好友甲"]["has_conversation"] is False)

# ── 3. compute_pending：预测名单与通道判定一致 ───────────────
seed_ledger([
    entry("手动好友", selected=True, channel="none"),            # 核心场景
    entry("创作者好友", selected=True, channel="creator"),       # 通道 B（未开开关）
    entry("同步好友", selected=True, has_conversation=True),
    entry("未勾选好友", selected=False, has_conversation=True),
])
cfg = {"allow_first_message": False, "first_message_daily_limit": 1}
pending = {e["display_name"]: e["send_channel"] for e in compute_pending(cfg)}
check("T8 手动好友进入预测名单且走通道A【核心修复】",
      pending.get("手动好友") == "consumer")
check("T9 creator好友未开开关 → 不在预测名单", "创作者好友" not in pending)
check("T10 同步好友走通道A", pending.get("同步好友") == "consumer")
check("T11 未勾选好友不发送", "未勾选好友" not in pending)

cfg_on = {"allow_first_message": True, "first_message_daily_limit": 5}
pending_on = {e["display_name"]: e["send_channel"] for e in compute_pending(cfg_on)}
check("T12 开启开关后 creator好友走通道B", pending_on.get("创作者好友") == "creator")

# ── 4. run_send 实发分流（全打桩，不启动浏览器）──────────────
import core.automation as auto

calls = {"consumer": [], "creator": []}
orig = {
    "open_browser": auto.open_browser,
    "_open_chat_page": auto._open_chat_page,
    "check_login": auto.check_login,
    "_send_consumer": auto._send_consumer,
    "_send_creator": auto._send_creator,
    "MessageSendTracker": auto.MessageSendTracker,
    "load_config": auto.load_config,
    "load_runtime": auto.load_runtime,
    "account_state_path": auto.account_state_path,
    "sleep": auto.time.sleep,
}


class FakePage:
    url = "https://www.douyin.com/falcon/webcast_im/chat"


class FakeCtx:
    def __enter__(self):
        return (None, None, None, FakePage())

    def __exit__(self, *a):
        return False


def fake_send_consumer(page, entry_, msg, dry_run, result, account_id=None, tracker=None):
    calls["consumer"].append(entry_["display_name"])
    result["ok"].append(entry_["display_name"])


def fake_send_creator(entry_, msg, dry_run, result, p, account_id=None):
    calls["creator"].append(entry_["display_name"])
    result["ok"].append(entry_["display_name"])


state_file = tmp / "state.json"
state_file.write_text('{"cookies": [], "origins": []}', encoding="utf-8")

auto.open_browser = lambda state_path=None: FakeCtx()
auto._open_chat_page = lambda page: True
auto.check_login = lambda page: (True, "ok")
auto._send_consumer = fake_send_consumer
auto._send_creator = fake_send_creator
auto.MessageSendTracker = lambda page: type("T", (), {"close": staticmethod(lambda: None)})()
auto.load_config = lambda account_id=None: {"messages": ["🔥"], "send_gap_min": 0, "send_gap_max": 0}
auto.load_runtime = lambda account_id=None: {}
auto.account_state_path = lambda account_id=None: state_file
auto.time.sleep = lambda s: None

try:
    # 4a. 混合名单：手动好友 + creator-only 好友
    seed_ledger([
        entry("手动好友乙", selected=True, channel="none"),
        entry("创作者好友丙", selected=True, channel="creator"),
    ])
    result = run_send(dry_run=False)
    check("T13 run_send 手动好友走通道A实发【核心修复】", "手动好友乙" in calls["consumer"])
    check("T14 run_send 手动好友未被通道B拦截", "手动好友乙" not in calls["creator"])
    check("T15 run_send creator-only好友仍走通道B", "创作者好友丙" in calls["creator"])
    check("T16 run_send 全部成功入账", len(result["ok"]) == 2 and not result["failed"] and not result["skipped"])

    # 4b. 修复前的受害者场景：只有手动好友（曾经 100% 被 skipped）
    seed_ledger([entry("存量手动好友", selected=True, channel="none")])
    calls["consumer"].clear()
    calls["creator"].clear()
    result = run_send(dry_run=False)
    check("T17 存量手动好友（旧数据channel=none）同样修复", "存量手动好友" in calls["consumer"])
    check("T18 不再产生 skipped", not result["skipped"])

    # 4c. only_names 过滤仍生效
    seed_ledger([
        entry("目标A", selected=True, channel="none"),
        entry("目标B", selected=True, channel="none"),
    ])
    calls["consumer"].clear()
    run_send(dry_run=False, only_names=["目标B"])
    check("T19 only_names 指定发送仍然生效", calls["consumer"] == ["目标B"])
finally:
    # 恢复所有打桩
    auto.open_browser = orig["open_browser"]
    auto._open_chat_page = orig["_open_chat_page"]
    auto.check_login = orig["check_login"]
    auto._send_consumer = orig["_send_consumer"]
    auto._send_creator = orig["_send_creator"]
    auto.MessageSendTracker = orig["MessageSendTracker"]
    auto.load_config = orig["load_config"]
    auto.load_runtime = orig["load_runtime"]
    auto.account_state_path = orig["account_state_path"]
    auto.time.sleep = orig["sleep"]
    ledger.account_dir = orig_account_dir

print()
print(f"结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
