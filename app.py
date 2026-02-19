from flask import Flask, request, redirect, url_for, render_template_string, abort, jsonify
import time
import secrets
import random
from dataclasses import dataclass

app = Flask(__name__)

# =============================
# CONFIG (你只需要改这里)
# =============================
TEACHER_KEY = "CHANGE_ME_TO_SOMETHING_SECRET"
PORT = 8000

# =============================
# Model / State
# =============================
@dataclass
class Params:
    deposits_per_student: float = 10.0
    liquid_reserve_ratio: float = 0.30
    long_asset_return: float = 1.50
    fire_sale_price: float = 0.60

    bad_news: bool = False
    news_severity: float = 0.20

    deposit_insurance: bool = False
    insurance_cap: float = 10.0

    lender_of_last_resort: bool = False
    lolr_limit: float = 0.0

    rounds_total: int = 6
    round_seconds: int = 40
    min_players_to_start: int = 8

    locked: bool = False

    # New toggles
    queue_mode: bool = True        # First-come-first-served queue
    show_withdraw_count: bool = True  # public signal during collect
    panic_sensitivity: float = 0.35   # higher withdrawal ratio lowers long-asset value
    private_signal_precision: float = 0.75
    bad_news_prob: float = 0.35

    def perceived_R(self) -> float:
        if not self.bad_news:
            return self.long_asset_return
        sev = max(0.0, min(1.0, self.news_severity))
        return max(0.0, self.long_asset_return * (1.0 - sev))


@dataclass
class Player:
    pid: str
    name: str
    joined_at: float
    last_choice: str = ""            # "W" or "S"
    choice_time: float | None = None # timestamp when they last chose (for queue)
    total_payoff: float = 0.0


STATE = {
    "params": Params(),
    "players": {},      # pid -> Player
    "phase": "lobby",   # lobby | collect | reveal | finished
    "round_no": 0,
    "deadline": 0.0,
    "history": [],      # list[dict] round outcomes
    "round_state": {
        "fundamental_bad": False,
        "fundamental_R": 1.0,
        "private_signals": {},  # pid -> "BAD" | "GOOD"
    },
}


def now() -> float:
    return time.time()


def total_players() -> int:
    return len(STATE["players"])


def reset_choices():
    for p in STATE["players"].values():
        p.last_choice = ""
        p.choice_time = None


def all_choices_in() -> bool:
    ps = list(STATE["players"].values())
    if not ps:
        return False
    return all(p.last_choice in ("W", "S") for p in ps)


def current_balance():
    """
    cash and long assets at start of current round.
    We carry over from last round.
    """
    P = STATE["params"]
    N = max(1, total_players())
    deposits_total = N * P.deposits_per_student
    cash0 = P.liquid_reserve_ratio * deposits_total
    long0 = (1.0 - P.liquid_reserve_ratio) * deposits_total

    if not STATE["history"]:
        return cash0, long0
    last = STATE["history"][-1]
    return float(last["cash_end"]), float(last["long_assets_remaining"])


def apply_insurance(x: float) -> float:
    P = STATE["params"]
    if not P.deposit_insurance:
        return x
    return min(x, max(0.0, P.insurance_cap))


def start_round():
    P = STATE["params"]
    if total_players() < P.min_players_to_start:
        return False, f"Need at least {P.min_players_to_start} players."
    if STATE["round_no"] >= P.rounds_total:
        STATE["phase"] = "finished"
        return True, "Game finished."

    STATE["round_no"] += 1
    STATE["phase"] = "collect"
    STATE["deadline"] = now() + P.round_seconds

    # Fundamental + private noisy signals (global games flavor)
    fundamental_bad = random.random() < max(0.0, min(1.0, P.bad_news_prob))
    fundamental_R = P.long_asset_return * (1.0 - P.news_severity) if fundamental_bad else P.long_asset_return
    precision = max(0.5, min(0.99, P.private_signal_precision))
    private_signals = {}
    for pid in STATE["players"]:
        if random.random() < precision:
            private_signals[pid] = "BAD" if fundamental_bad else "GOOD"
        else:
            private_signals[pid] = "GOOD" if fundamental_bad else "BAD"

    STATE["round_state"] = {
        "fundamental_bad": fundamental_bad,
        "fundamental_R": max(0.0, fundamental_R),
        "private_signals": private_signals,
    }
    reset_choices()
    return True, f"Round {STATE['round_no']} started."


def maybe_auto_reveal():
    if STATE["phase"] == "collect" and (now() >= STATE["deadline"] or all_choices_in()):
        reveal_round()


def reveal_round():
    if STATE["phase"] != "collect":
        return

    P = STATE["params"]
    players = list(STATE["players"].values())
    N = len(players)
    if N == 0:
        STATE["phase"] = "finished"
        return

    # Aggregate counts
    W_players = [p for p in players if p.last_choice == "W"]
    S_players = [p for p in players if p.last_choice == "S"]
    W = len(W_players)
    S = len(S_players)

    cash_start, long_assets = current_balance()
    cash = cash_start

    # LoLR cash injection
    lolr_used = 0.0
    if P.lender_of_last_resort and P.lolr_limit > 0:
        lolr_used = P.lolr_limit
        cash += lolr_used

    # Need to potentially fire-sale long assets to meet withdrawal demand
    total_demand = W * P.deposits_per_student

    long_sold = 0.0
    if total_demand > cash:
        gap = total_demand - cash
        fs = max(1e-9, min(1.0, P.fire_sale_price))
        needed_sale = gap / fs
        long_sold = min(long_assets, needed_sale)
        cash += long_sold * fs
        long_assets -= long_sold

    # Now pay withdrawals (either queue or pro-rata)
    bank_default = total_demand > cash + 1e-9  # not enough to pay everyone fully

    payout_map = {}  # pid -> payout in this round

    if P.queue_mode:
        # --- First-come-first-served queue ---
        # Sort withdrawers by choice_time (earlier gets served first). If someone has no timestamp, treat as late.
        def ts(p: Player):
            return p.choice_time if p.choice_time is not None else 10**18

        W_sorted = sorted(W_players, key=ts)

        full = P.deposits_per_student
        for wp in W_sorted:
            if cash >= full:
                payout = full
                cash -= full
            else:
                # last withdrawer may get partial; after that, nothing
                payout = max(0.0, cash)
                cash = 0.0
            payout_map[wp.pid] = apply_insurance(payout)

        # Stayers: expected claim value on remaining assets (fundamentals + panic externality)
        fundamental_R = STATE["round_state"].get("fundamental_R", P.perceived_R())
        withdrawal_ratio = (W / N) if N > 0 else 0.0
        panic_discount = max(0.0, 1.0 - P.panic_sensitivity * withdrawal_ratio)
        R = max(0.0, fundamental_R * panic_discount)
        total_future_value = cash + long_assets * R
        per_stayer = (total_future_value / S) if S > 0 else 0.0
        per_stayer = apply_insurance(per_stayer)
        for sp in S_players:
            payout_map[sp.pid] = per_stayer

        payout_W_avg = (sum(payout_map[p.pid] for p in W_players) / W) if W > 0 else 0.0
        payout_S_val = per_stayer

        note = f"Queue mode. LoLR +{lolr_used:.2f}. Fundamental R={fundamental_R:.2f}, effective R={R:.2f}."
        cash_end = cash
        long_remaining = long_assets

    else:
        # --- Pro-rata rationing (older version) ---
        if not bank_default:
            payout_W = apply_insurance(P.deposits_per_student)
            cash -= total_demand

            fundamental_R = STATE["round_state"].get("fundamental_R", P.perceived_R())
            withdrawal_ratio = (W / N) if N > 0 else 0.0
            panic_discount = max(0.0, 1.0 - P.panic_sensitivity * withdrawal_ratio)
            R = max(0.0, fundamental_R * panic_discount)
            total_future_value = cash + long_assets * R
            payout_S = apply_insurance((total_future_value / S) if S > 0 else 0.0)

            for wp in W_players:
                payout_map[wp.pid] = payout_W
            for sp in S_players:
                payout_map[sp.pid] = payout_S

            note = f"No default. LoLR +{lolr_used:.2f}. Fundamental R={fundamental_R:.2f}, effective R={R:.2f}."
            cash_end = cash
            long_remaining = long_assets
            payout_W_avg = payout_W
            payout_S_val = payout_S
        else:
            # ration withdrawers pro-rata
            payout_W = apply_insurance((cash / W) if W > 0 else 0.0)
            for wp in W_players:
                payout_map[wp.pid] = payout_W
            for sp in S_players:
                payout_map[sp.pid] = 0.0
            note = f"DEFAULT (pro-rata). LoLR +{lolr_used:.2f}."
            cash_end = 0.0
            long_remaining = long_assets
            payout_W_avg = payout_W
            payout_S_val = 0.0
            cash = 0.0

    # Update totals
    for p in players:
        p.total_payoff += payout_map.get(p.pid, 0.0)

    # Diagnostics: how many got full vs partial in queue mode
    queue_stats = ""
    if P.queue_mode and W > 0:
        full_cnt = sum(1 for wp in W_players if abs(payout_map.get(wp.pid, 0.0) - apply_insurance(P.deposits_per_student)) < 1e-9)
        partial_cnt = sum(1 for wp in W_players if 1e-9 < payout_map.get(wp.pid, 0.0) < apply_insurance(P.deposits_per_student) - 1e-9)
        zero_cnt = W - full_cnt - partial_cnt
        queue_stats = f" | Served: full={full_cnt}, partial={partial_cnt}, zero={zero_cnt}"

    STATE["history"].append({
        "round_no": STATE["round_no"],
        "withdrawals": W,
        "stays": S,
        "total_players": N,
        "bank_default": bank_default,
        "payout_withdraw_avg": float(payout_W_avg),
        "payout_stay": float(payout_S_val),
        "cash_start": float(cash_start),
        "cash_end": float(cash_end),
        "long_assets_remaining": float(long_remaining),
        "long_assets_sold": float(long_sold),
        "note": note + queue_stats,
        # extra: list of (name, payout) for withdrawers to show “early wins”
        "withdraw_queue": [
            {"name": wp.name, "t": (wp.choice_time or 0.0), "payout": float(payout_map.get(wp.pid, 0.0))}
            for wp in sorted(W_players, key=lambda x: x.choice_time if x.choice_time is not None else 10**18)
        ] if P.queue_mode else [],
        "fundamental_bad": STATE["round_state"].get("fundamental_bad", False),
        "fundamental_R": float(STATE["round_state"].get("fundamental_R", P.perceived_R())),
    })

    if STATE["round_no"] >= P.rounds_total:
        STATE["phase"] = "finished"
    else:
        STATE["phase"] = "reveal"


# =============================
# UI
# =============================
CSS = """
<style>
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif;margin:22px;}
.card{border:1px solid #ddd;border-radius:14px;padding:14px;margin:12px 0;}
.btn{display:inline-block;padding:10px 14px;border-radius:12px;border:1px solid #333;text-decoration:none;color:#111;margin-right:8px;}
.btn.primary{background:#111;color:#fff;}
.btn.danger{border-color:#b00020;color:#b00020;}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;border:1px solid #ccc;font-size:12px;margin-left:8px;}
.badge.red{border-color:#b00020;color:#b00020;}
.badge.green{border-color:#0a7a2f;color:#0a7a2f;}
small.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;}
table{border-collapse:collapse;width:100%;}
th,td{border-bottom:1px solid #eee;padding:7px;text-align:left;}
</style>
"""

JOIN_PAGE = """
<!doctype html><html><head><meta charset="utf-8"><title>Join</title>{{css|safe}}</head><body>
<h2>🏦 Bank Run Game (H5)</h2>
<div class="card">
  <form method="post" action="/join">
    <label>你的名字/学号： <input name="name" maxlength="32" required></label>
    <button class="btn primary" type="submit">加入</button>
  </form>
  <p><small>老师已设置参数（只读）。每轮你选择 Withdraw(取款) 或 Stay(不取)。</small></p>
</div>
</body></html>
"""

STUDENT_PAGE = """
<!doctype html><html><head><meta charset="utf-8"><title>Play</title>{{css|safe}}</head><body>
<h2>🏦 Bank Run Game</h2>

<div class="card">
  <div><b>你：</b>{{name}}</div>
  <div><b>Round：</b>{{round_no}} / {{params.rounds_total}} &nbsp; <b>Phase：</b>{{phase}}</div>
  <div><b>Players：</b>{{n_players}} &nbsp; <b>Bad news：</b>{{"ON" if params.bad_news else "OFF"}}
    &nbsp; <b>Insurance：</b>{{"ON" if params.deposit_insurance else "OFF"}}
    &nbsp; <b>LoLR：</b>{{"ON" if params.lender_of_last_resort else "OFF"}}
    &nbsp; <b>Queue：</b>{{"ON" if params.queue_mode else "OFF"}}
  </div>
  {% if phase in ("collect", "reveal", "finished") %}
    <div style="margin-top:8px;"><b>你的私人信号：</b>
      {% if private_signal == "BAD" %}
        ⚠️ 你听到“坏消息”（但可能是噪音）
      {% elif private_signal == "GOOD" %}
        ✅ 你听到“正常消息”（但可能是噪音）
      {% else %}
        -
      {% endif %}
    </div>
  {% endif %}
  {% if params.show_withdraw_count and phase=="collect" %}
    <div style="margin-top:8px;">
      <b>Public signal:</b> Current withdrawals = <b id="wcnt">...</b> / {{n_players}}
    </div>
  {% endif %}
</div>

<div class="card">
  <h3>本局参数（只读）</h3>
  <table>
    <tr><th>Parameter</th><th>Value</th></tr>
    <tr><td>deposits_per_student</td><td>{{"%.2f"|format(params.deposits_per_student)}}</td></tr>
    <tr><td>liquid_reserve_ratio</td><td>{{"%.2f"|format(params.liquid_reserve_ratio)}}</td></tr>
    <tr><td>long_asset_return</td><td>{{"%.2f"|format(params.long_asset_return)}}</td></tr>
    <tr><td>fire_sale_price</td><td>{{"%.2f"|format(params.fire_sale_price)}}</td></tr>
    <tr><td>bad_news</td><td>{{"ON" if params.bad_news else "OFF"}}</td></tr>
    <tr><td>news_severity</td><td>{{"%.2f"|format(params.news_severity)}}</td></tr>
    <tr><td>insurance_cap</td><td>{{"%.2f"|format(params.insurance_cap)}}</td></tr>
    <tr><td>lolr_limit</td><td>{{"%.2f"|format(params.lolr_limit)}}</td></tr>
    <tr><td>Perceived R</td><td>{{"%.2f"|format(params.perceived_R())}}</td></tr>
    <tr><td>Queue mode</td><td>{{"ON" if params.queue_mode else "OFF"}}</td></tr>
    <tr><td>Show withdraw count</td><td>{{"ON" if params.show_withdraw_count else "OFF"}}</td></tr>
    <tr><td>Panic sensitivity</td><td>{{"%.2f"|format(params.panic_sensitivity)}}</td></tr>
    <tr><td>Private signal precision</td><td>{{"%.2f"|format(params.private_signal_precision)}}</td></tr>
    <tr><td>Fundamental bad-news prob</td><td>{{"%.2f"|format(params.bad_news_prob)}}</td></tr>
  </table>
</div>

{% if phase == "lobby" %}
  <div class="card">
    <p>等待老师开始。请保持页面打开。</p>
    <p><a class="btn" href="/s/{{pid}}">刷新</a></p>
  </div>

{% elif phase == "collect" %}
  <div class="card">
    <p><b>本轮选择：</b></p>
    <p>
      <a class="btn danger" href="/choose?pid={{pid}}&c=W">Withdraw（取款）</a>
      <a class="btn primary" href="/choose?pid={{pid}}&c=S">Stay（不取）</a>
    </p>
    <p>你当前选择： <b>{{choice or "(未选)"}}</b></p>
    <p>剩余时间： <b id="t"></b> 秒</p>
    {% if params.queue_mode %}
      <p><small>提示：队列模式下“先点 Withdraw 的人更可能拿到全额”。</small></p>
    {% endif %}
  </div>

  <script>
    const deadline = {{deadline}};
    const showWithdraw = {{ "true" if params.show_withdraw_count else "false" }};
    async function refreshSignal(){
      if(!showWithdraw) return;
      try{
        const r = await fetch("/signal");
        const j = await r.json();
        const el = document.getElementById("wcnt");
        if(el) el.innerText = j.withdrawals;
      }catch(e){}
    }
    function tick(){
      const left = Math.max(0, Math.floor(deadline - Date.now()/1000));
      document.getElementById("t").innerText = left;
      if(left<=0) location.reload();
    }
    setInterval(tick, 500); tick();
    refreshSignal();
    setInterval(refreshSignal, 800);
  </script>

{% else %}
  <div class="card">
    <p><b>上一轮结果</b>
      {% if last and last.bank_default %}<span class="badge red">DEFAULT risk</span>{% endif %}
      {% if last and not last.bank_default %}<span class="badge green">NO DEFAULT</span>{% endif %}
    </p>
    {% if last %}
      <p>Withdrawals：<b>{{last.withdrawals}}</b> / {{last.total_players}}</p>
      <p>Avg payout(Withdraw)：<b>{{"%.2f"|format(last.payout_withdraw_avg)}}</b> |
         Payout(Stay)：<b>{{"%.2f"|format(last.payout_stay)}}</b></p>
      <p><small>{{last.note}}</small></p>

      {% if last.withdraw_queue and last.withdraw_queue|length > 0 %}
        <h4>Withdraw queue (早取更安全)</h4>
        <table>
          <tr><th>Order</th><th>Name</th><th>Payout</th></tr>
          {% for item in last.withdraw_queue %}
            <tr><td>{{loop.index}}</td><td>{{item.name}}</td><td>{{"%.2f"|format(item.payout)}}</td></tr>
          {% endfor %}
        </table>
      {% endif %}
    {% else %}
      <p>暂无结果。</p>
    {% endif %}
    <p><a class="btn" href="/s/{{pid}}">刷新</a></p>
  </div>

  <div class="card">
    <p><b>你的累计收益：</b>{{"%.2f"|format(total_payoff)}}</p>
  </div>
{% endif %}

<div class="card">
  <p><b>课堂要点（多重 Nash）：</b></p>
  <ul>
    <li>你预期别人会取 → 取款更安全（尤其队列先到先得）。</li>
    <li>你预期别人不取 → 不取更好（避免火售损失）。</li>
    <li>公共信号（当前取款人数）会放大恐慌，导致自我实现的挤兑。</li>
    <li>私人信号不完美：你听到的消息可能有噪音，关键在于你如何预判“别人会怎么想”。</li>
  </ul>
</div>

</body></html>
"""

TEACHER_PAGE = """
<!doctype html><html><head><meta charset="utf-8"><title>Teacher</title>{{css|safe}}</head><body>
<h2>🎓 Teacher Console (Bank)</h2>

<div class="card">
  <p><b>Student link:</b> <small class="mono">{{host}}/</small></p>
  <p>Players: <b>{{n_players}}</b> | Phase: <b>{{phase}}</b> | Round: <b>{{round_no}}</b> / {{params.rounds_total}}</p>
  <p>Params locked: <b>{{"YES" if params.locked else "NO"}}</b></p>
  <p>
    <a class="btn primary" href="/teacher/action?key={{key}}&a=start">Start / Next Round</a>
    <a class="btn" href="/teacher/action?key={{key}}&a=reveal">Force Reveal</a>
    <a class="btn" href="/teacher/action?key={{key}}&a=lock">{{"Unlock Params" if params.locked else "Lock Params"}}</a>
    <a class="btn danger" href="/teacher/action?key={{key}}&a=reset" onclick="return confirm('Reset?');">Reset</a>
  </p>
  {% if phase=="collect" %}
    <p>Time left: <b id="t"></b> sec | Public withdrawals now: <b id="tw">...</b></p>
  {% endif %}
</div>

<div class="card">
  <h3>Set parameters (teacher only)</h3>
  {% if params.locked %}
    <p><b>Locked.</b> 解锁后才能修改参数。</p>
  {% endif %}
  <form method="post" action="/teacher/params?key={{key}}">
    <table>
      <tr><th>Parameter</th><th>Value</th><th>Note</th></tr>
      <tr><td>deposits_per_student</td><td><input name="deposits_per_student" value="{{params.deposits_per_student}}" {% if params.locked %}disabled{% endif %}></td><td>每人存款</td></tr>
      <tr><td>liquid_reserve_ratio</td><td><input name="liquid_reserve_ratio" value="{{params.liquid_reserve_ratio}}" {% if params.locked %}disabled{% endif %}></td><td>现金准备金比例</td></tr>
      <tr><td>long_asset_return</td><td><input name="long_asset_return" value="{{params.long_asset_return}}" {% if params.locked %}disabled{% endif %}></td><td>长期资产到期回报</td></tr>
      <tr><td>fire_sale_price</td><td><input name="fire_sale_price" value="{{params.fire_sale_price}}" {% if params.locked %}disabled{% endif %}></td><td>火售价格</td></tr>

      <tr><td>bad_news</td><td>
        <select name="bad_news" {% if params.locked %}disabled{% endif %}>
          <option value="0" {% if not params.bad_news %}selected{% endif %}>OFF</option>
          <option value="1" {% if params.bad_news %}selected{% endif %}>ON</option>
        </select>
      </td><td>坏消息开关</td></tr>

      <tr><td>news_severity</td><td><input name="news_severity" value="{{params.news_severity}}" {% if params.locked %}disabled{% endif %}></td><td>坏消息强度</td></tr>

      <tr><td>deposit_insurance</td><td>
        <select name="deposit_insurance" {% if params.locked %}disabled{% endif %}>
          <option value="0" {% if not params.deposit_insurance %}selected{% endif %}>OFF</option>
          <option value="1" {% if params.deposit_insurance %}selected{% endif %}>ON</option>
        </select>
      </td><td>存款保险</td></tr>

      <tr><td>insurance_cap</td><td><input name="insurance_cap" value="{{params.insurance_cap}}" {% if params.locked %}disabled{% endif %}></td><td>保险上限</td></tr>

      <tr><td>lender_of_last_resort</td><td>
        <select name="lender_of_last_resort" {% if params.locked %}disabled{% endif %}>
          <option value="0" {% if not params.lender_of_last_resort %}selected{% endif %}>OFF</option>
          <option value="1" {% if params.lender_of_last_resort %}selected{% endif %}>ON</option>
        </select>
      </td><td>最后贷款人</td></tr>

      <tr><td>lolr_limit</td><td><input name="lolr_limit" value="{{params.lolr_limit}}" {% if params.locked %}disabled{% endif %}></td><td>注入现金</td></tr>

      <tr><td>rounds_total</td><td><input name="rounds_total" value="{{params.rounds_total}}" {% if params.locked %}disabled{% endif %}></td><td>总轮数</td></tr>
      <tr><td>round_seconds</td><td><input name="round_seconds" value="{{params.round_seconds}}" {% if params.locked %}disabled{% endif %}></td><td>每轮秒数</td></tr>
      <tr><td>min_players_to_start</td><td><input name="min_players_to_start" value="{{params.min_players_to_start}}" {% if params.locked %}disabled{% endif %}></td><td>最少人数</td></tr>

      <tr><td>queue_mode</td><td>
        <select name="queue_mode" {% if params.locked %}disabled{% endif %}>
          <option value="1" {% if params.queue_mode %}selected{% endif %}>ON</option>
          <option value="0" {% if not params.queue_mode %}selected{% endif %}>OFF</option>
        </select>
      </td><td>先到先得队列（越早Withdraw越安全）</td></tr>

      <tr><td>show_withdraw_count</td><td>
        <select name="show_withdraw_count" {% if params.locked %}disabled{% endif %}>
          <option value="1" {% if params.show_withdraw_count %}selected{% endif %}>ON</option>
          <option value="0" {% if not params.show_withdraw_count %}selected{% endif %}>OFF</option>
        </select>
      </td><td>学生实时看到“已取款人数”</td></tr>

      <tr><td>panic_sensitivity</td><td><input name="panic_sensitivity" value="{{params.panic_sensitivity}}" {% if params.locked %}disabled{% endif %}></td><td>恐慌外部性（W比例越高，长期资产有效回报越低）</td></tr>
      <tr><td>private_signal_precision</td><td><input name="private_signal_precision" value="{{params.private_signal_precision}}" {% if params.locked %}disabled{% endif %}></td><td>私人信号准确率（0.5~0.99）</td></tr>
      <tr><td>bad_news_prob</td><td><input name="bad_news_prob" value="{{params.bad_news_prob}}" {% if params.locked %}disabled{% endif %}></td><td>每轮基本面坏消息概率</td></tr>
    </table>
    <p><button class="btn primary" type="submit" {% if params.locked %}disabled{% endif %}>Save</button></p>
  </form>
</div>

<div class="card">
  <h3>Players</h3>
  <table>
    <tr><th>Name</th><th>Choice</th><th>Total payoff</th></tr>
    {% for p in players %}
      <tr><td>{{p.name}}</td><td>{{p.last_choice}}</td><td>{{"%.2f"|format(p.total_payoff)}}</td></tr>
    {% endfor %}
  </table>
</div>

<div class="card">
  <h3>Current Round Fundamentals (for debrief)</h3>
  <p>Bad fundamental state: <b>{{"YES" if round_state.fundamental_bad else "NO"}}</b> |
     Fundamental R: <b>{{"%.2f"|format(round_state.fundamental_R)}}</b></p>
</div>

<div class="card">
  <h3>History</h3>
  <table>
    <tr><th>Round</th><th>W</th><th>S</th><th>Default risk</th><th>AvgPayW</th><th>PayS</th><th>Fund.Bad</th><th>Fund.R</th><th>CashStart</th><th>CashEnd</th><th>LongLeft</th><th>Note</th></tr>
    {% for h in history %}
      <tr>
        <td>{{h.round_no}}</td>
        <td>{{h.withdrawals}}</td>
        <td>{{h.stays}}</td>
        <td>{{"YES" if h.bank_default else "NO"}}</td>
        <td>{{"%.2f"|format(h.payout_withdraw_avg)}}</td>
        <td>{{"%.2f"|format(h.payout_stay)}}</td>
        <td>{{"YES" if h.fundamental_bad else "NO"}}</td>
        <td>{{"%.2f"|format(h.fundamental_R)}}</td>
        <td>{{"%.2f"|format(h.cash_start)}}</td>
        <td>{{"%.2f"|format(h.cash_end)}}</td>
        <td>{{"%.2f"|format(h.long_assets_remaining)}}</td>
        <td><small>{{h.note}}</small></td>
      </tr>
    {% endfor %}
  </table>
</div>

{% if phase=="collect" %}
<script>
  const deadline = {{deadline}};
  async function refresh(){
    try{
      const r = await fetch("/signal");
      const j = await r.json();
      const el = document.getElementById("tw");
      if(el) el.innerText = j.withdrawals;
    }catch(e){}
  }
  function tick(){
    const left = Math.max(0, Math.floor(deadline - Date.now()/1000));
    const el = document.getElementById("t");
    if(el) el.innerText = left;
    if(left<=0) location.reload();
  }
  setInterval(tick, 500); tick();
  refresh(); setInterval(refresh, 800);
</script>
{% endif %}

</body></html>
"""


def require_teacher():
    key = request.args.get("key", "")
    if key != TEACHER_KEY:
        abort(403)


@app.route("/", methods=["GET"])
def index():
    maybe_auto_reveal()
    return render_template_string(JOIN_PAGE, css=CSS)


@app.route("/join", methods=["POST"])
def join():
    name = (request.form.get("name") or "").strip()[:32]
    if not name:
        return redirect(url_for("index"))
    pid = secrets.token_urlsafe(8)
    STATE["players"][pid] = Player(pid=pid, name=name, joined_at=now())
    return redirect(url_for("student", pid=pid))


@app.route("/s/<pid>", methods=["GET"])
def student(pid):
    maybe_auto_reveal()
    p = STATE["players"].get(pid)
    if not p:
        return redirect(url_for("index"))

    P = STATE["params"]
    last = STATE["history"][-1] if STATE["history"] else None

    return render_template_string(
        STUDENT_PAGE,
        css=CSS,
        pid=pid,
        name=p.name,
        choice=p.last_choice,
        total_payoff=p.total_payoff,
        private_signal=STATE["round_state"].get("private_signals", {}).get(pid),
        params=P,
        phase=STATE["phase"],
        round_no=STATE["round_no"],
        n_players=total_players(),
        deadline=STATE["deadline"],
        last=last,
    )


@app.route("/choose", methods=["GET"])
def choose():
    maybe_auto_reveal()
    pid = request.args.get("pid", "")
    c = (request.args.get("c", "") or "").upper()
    if STATE["phase"] != "collect":
        return redirect(url_for("student", pid=pid))

    p = STATE["players"].get(pid)
    if not p:
        return redirect(url_for("index"))

    if c not in ("W", "S"):
        return redirect(url_for("student", pid=pid))

    # record timestamp when choosing Withdraw (or Stay if you want; we only need W for queue)
    p.last_choice = c
    p.choice_time = now()
    return redirect(url_for("student", pid=pid))


@app.route("/signal", methods=["GET"])
def signal():
    """
    Public signal endpoint for live withdrawals count.
    Used by both student and teacher page JS polling.
    """
    maybe_auto_reveal()
    if STATE["phase"] != "collect":
        return jsonify({"phase": STATE["phase"], "withdrawals": 0, "players": total_players()})
    w = sum(1 for p in STATE["players"].values() if p.last_choice == "W")
    return jsonify({"phase": STATE["phase"], "withdrawals": w, "players": total_players()})


@app.route("/teacher", methods=["GET"])
def teacher():
    maybe_auto_reveal()
    require_teacher()
    host = request.host_url.rstrip("/")
    P = STATE["params"]
    return render_template_string(
        TEACHER_PAGE,
        css=CSS,
        host=host,
        key=TEACHER_KEY,
        params=P,
        phase=STATE["phase"],
        round_no=STATE["round_no"],
        deadline=STATE["deadline"],
        n_players=total_players(),
        players=sorted(STATE["players"].values(), key=lambda x: x.joined_at),
        history=STATE["history"],
        round_state=STATE["round_state"],
    )


@app.route("/teacher/params", methods=["POST"])
def teacher_params():
    require_teacher()
    P = STATE["params"]
    if P.locked:
        return redirect(url_for("teacher", key=TEACHER_KEY))

    def f(k, default):
        try:
            return float(request.form.get(k, default))
        except Exception:
            return float(default)

    def i(k, default):
        try:
            return int(float(request.form.get(k, default)))
        except Exception:
            return int(default)

    def b(k):
        return request.form.get(k, "0") in ("1", "true", "True", "on")

    P.deposits_per_student = max(0.0, f("deposits_per_student", P.deposits_per_student))
    P.liquid_reserve_ratio = max(0.0, min(1.0, f("liquid_reserve_ratio", P.liquid_reserve_ratio)))
    P.long_asset_return = max(0.0, f("long_asset_return", P.long_asset_return))
    P.fire_sale_price = max(1e-6, min(1.0, f("fire_sale_price", P.fire_sale_price)))

    P.bad_news = b("bad_news")
    P.news_severity = max(0.0, min(1.0, f("news_severity", P.news_severity)))

    P.deposit_insurance = b("deposit_insurance")
    P.insurance_cap = max(0.0, f("insurance_cap", P.insurance_cap))

    P.lender_of_last_resort = b("lender_of_last_resort")
    P.lolr_limit = max(0.0, f("lolr_limit", P.lolr_limit))

    P.rounds_total = max(1, i("rounds_total", P.rounds_total))
    P.round_seconds = max(10, i("round_seconds", P.round_seconds))
    P.min_players_to_start = max(1, i("min_players_to_start", P.min_players_to_start))

    # New toggles
    P.queue_mode = b("queue_mode")
    P.show_withdraw_count = b("show_withdraw_count")
    P.panic_sensitivity = max(0.0, min(1.5, f("panic_sensitivity", P.panic_sensitivity)))
    P.private_signal_precision = max(0.5, min(0.99, f("private_signal_precision", P.private_signal_precision)))
    P.bad_news_prob = max(0.0, min(1.0, f("bad_news_prob", P.bad_news_prob)))

    return redirect(url_for("teacher", key=TEACHER_KEY))


@app.route("/teacher/action", methods=["GET"])
def teacher_action():
    require_teacher()
    action = request.args.get("a", "")
    P = STATE["params"]

    if action == "start":
        start_round()
        maybe_auto_reveal()
    elif action == "reveal":
        reveal_round()
    elif action == "lock":
        P.locked = not P.locked
    elif action == "reset":
        # reset everything except params
        STATE["players"] = {}
        STATE["history"] = []
        STATE["phase"] = "lobby"
        STATE["round_no"] = 0
        STATE["deadline"] = 0.0
        STATE["round_state"] = {
            "fundamental_bad": False,
            "fundamental_R": 1.0,
            "private_signals": {},
        }

    return redirect(url_for("teacher", key=TEACHER_KEY))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
