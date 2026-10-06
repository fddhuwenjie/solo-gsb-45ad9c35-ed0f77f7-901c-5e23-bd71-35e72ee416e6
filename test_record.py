"""烧成实绩登记：业务规则、逐批判定、排程变动校验、冻结修改、并发与持久化验证。"""
import os
import tempfile
from threading import Thread

_tmp = tempfile.mkdtemp()
os.environ["KILN_DB_PATH"] = os.path.join(_tmp, "record.db")

from app import create_app

app = create_app()
c = app.test_client()

passed = []
def check(name, cond, detail=""):
    assert cond, f"FAIL: {name} {detail}"
    passed.append(name)

# ---------- 准备：批次 → 已确认窑次 → 烧成安排 ----------
def add_batch(code, pmin, pmax, hmin, hmax):
    r = c.post("/api/batches", json={
        "code": code, "peak_min": pmin, "peak_max": pmax,
        "hold_min": hmin, "hold_max": hmax})
    assert r.status_code == 201, r.data
    return r.get_json()

def confirm_load(name, kiln_id, batch_pos):
    d = c.post("/api/drafts", json={"name": name, "kiln_id": kiln_id}).get_json()
    for b, pos in batch_pos:
        c.put(f"/api/drafts/{d['id']}/items", json={"batch_id": b["id"], "pos_no": pos})
    p = c.get(f"/api/drafts/{d['id']}/preview").get_json()
    assert p["ready"], p
    snap = {"draft_version": p["draft_version"],
            "items": [{"batch_id": i["batch_id"], "batch_version": i["batch_version"],
                       "pos_no": i["pos_no"]} for i in p["items"]]}
    r = c.post(f"/api/drafts/{d['id']}/confirm", json=snap)
    assert r.status_code == 201, r.data
    return r.get_json()

def make_schedule(load_id, start, end, peak, hold):
    r = c.post("/api/schedules", json={
        "load_id": load_id, "start_at": start, "end_at": end,
        "peak_temp": peak, "hold_minutes": hold})
    assert r.status_code == 201, r.data
    return r.get_json()

def reg(schedule_id, actual_peak, actual_hold, sched):
    return c.post(f"/api/schedules/{schedule_id}/firing-record", json={
        "actual_peak_temp": actual_peak,
        "actual_hold_minutes": actual_hold,
        "schedule_start_at": sched["start_at"],
        "schedule_end_at": sched["end_at"],
        "schedule_peak_temp": sched["peak_temp"],
        "schedule_hold_minutes": sched["hold_minutes"]})

kilns = c.get("/api/kilns").get_json()
k1, k2 = kilns[0]["id"], kilns[1]["id"]

# 共同区间：峰 1220~1250，保温 30~35
ba = add_batch("BA", 1200, 1260, 20, 40)
bb = add_batch("BB", 1220, 1250, 30, 35)
load1 = confirm_load("L1", k1, [(ba, 1), (bb, 2)])
s1 = make_schedule(load1["id"],
                   "2026-10-08T08:00:00+08:00", "2026-10-08T12:00:00+08:00",
                   1240, 35)

bc = add_batch("BC", 1230, 1240, 25, 35)
load2 = confirm_load("L2", k1, [(bc, 1)])
s2 = make_schedule(load2["id"],
                   "2026-10-08T12:00:00+08:00", "2026-10-08T16:00:00+08:00",
                   1235, 30)

bd = add_batch("BD", 1200, 1300, 10, 60)
load3 = confirm_load("L3", k2, [(bd, 1)])
s3 = make_schedule(load3["id"],
                   "2026-10-09T08:00:00+08:00", "2026-10-09T12:00:00+08:00",
                   1250, 40)

# ---------- 1. 实测值合法性 ----------
r = reg(s1["id"], -1, 35, s1)
check("负峰温被拒", r.status_code == 400 and "负数" in r.get_json()["error"])
r = reg(s1["id"], 1240, -1, s1)
check("负保温被拒", r.status_code == 400 and "负数" in r.get_json()["error"])
r = reg(s1["id"], "热", 35, s1)
check("非数字峰温被拒", r.status_code == 400)
r = reg(s1["id"], 1240, 35.5, s1)
check("非整数保温被拒", r.status_code == 400 and "非负整数" in r.get_json()["error"])
r = reg(s1["id"], True, 35, s1)
check("布尔值不充当峰温", r.status_code == 400)
r = reg(s1["id"], 1240, True, s1)
check("布尔值不充当保温", r.status_code == 400)
r = reg(99999, 1240, 35, s1)
check("安排不存在 404", r.status_code == 404)

# ---------- 2. 登记必须携带安排时段与拟用参数 ----------
r = c.post(f"/api/schedules/{s1['id']}/firing-record", json={
    "actual_peak_temp": 1240, "actual_hold_minutes": 35})
check("缺少安排快照被拒", r.status_code == 400)
r = c.post(f"/api/schedules/{s1['id']}/firing-record", json={
    "actual_peak_temp": 1240, "actual_hold_minutes": 35,
    "schedule_start_at": "2026-10-08T00:00:00.000000Z",
    "schedule_end_at": "2026-10-08T04:00:00.000000Z",
    "schedule_peak_temp": 1240, "schedule_hold_minutes": 35.0})
check("拟用保温非整数被拒", r.status_code == 400)

# ---------- 3. 提交前安排有变动 → stale，不生成实绩 ----------
# 先把 s1 改到另一个不重叠时段，再用旧快照登记
r = c.put(f"/api/schedules/{s1['id']}", json={
    "start_at": "2026-10-10T08:00:00+08:00", "end_at": "2026-10-10T12:00:00+08:00",
    "peak_temp": 1240, "hold_minutes": 35})
assert r.status_code == 200, r.data
r = reg(s1["id"], 1240, 35, s1)   # s1 是修改前读到的旧安排
check("安排时段变动后旧快照登记被拒",
      r.status_code == 409 and r.get_json()["stale"] is True
      and "刷新" in r.get_json()["error"], r.data)
check("stale 返回最新安排",
      r.get_json()["latest"]["start_at"] == "2026-10-10T00:00:00.000000Z")
check("stale 不生成实绩",
      c.get("/api/firing-records").get_json() == [])
check("stale 后安排列表仍无 record_id",
      next(x for x in c.get("/api/schedules").get_json() if x["id"] == s1["id"])["record_id"] is None)

# 拟用参数变动也算 stale
s1b = c.get("/api/schedules").get_json()
s1b = next(x for x in s1b if x["id"] == s1["id"])
r = c.put(f"/api/schedules/{s1['id']}", json={
    "start_at": "2026-10-10T08:00:00+08:00", "end_at": "2026-10-10T12:00:00+08:00",
    "peak_temp": 1230, "hold_minutes": 32})
assert r.status_code == 200
r = reg(s1["id"], 1240, 35, s1b)
check("拟用参数变动后旧快照登记被拒", r.status_code == 409 and r.get_json()["stale"])

# ---------- 4. 正常登记：实测全部在界内（取两批共同范围内的值与边界） ----------
s1_now = next(x for x in c.get("/api/schedules").get_json() if x["id"] == s1["id"])
r = reg(s1["id"], 1220, 35, s1_now)   # BA[1200,1260]/[20,40]、BB[1220,1250]/[30,35]：均在界（含边界）
check("登记成功", r.status_code == 201, r.data)
rec1 = r.get_json()
check("记录登记时间", "registered_at" in rec1 and rec1["registered_at"])
check("实绩保存安排快照",
      (rec1["schedule_start_at"], rec1["schedule_end_at"],
       rec1["schedule_peak_temp"], rec1["schedule_hold_minutes"])
      == (s1_now["start_at"], s1_now["end_at"], 1230.0, 32))
check("实绩保存实测值",
      rec1["actual_peak_temp"] == 1220 and rec1["actual_hold_minutes"] == 35)
check("两批均判定（边界算在界内）",
      len(rec1["items"]) == 2 and all(i["in_range"] for i in rec1["items"]))
check("汇总无超界", rec1["all_in_range"] is True and rec1["out_of_range_count"] == 0)

# GET 详情
g = c.get(f"/api/firing-records/{rec1['id']}").get_json()
check("GET 实绩详情含拟用值与实测值",
      g["schedule_peak_temp"] == 1230 and g["items"][0]["actual_peak_temp"] == 1220
      and g["items"][0]["batch_code"] in ("BA", "BB"))

# ---------- 5. 超界实测值如实保存并逐批标出 ----------
# load2 只含 BC：峰[1230,1240] 保温[25,35]；实测峰 1245（超上界），保温 20（低下界）
r = reg(s2["id"], 1245, 20, s2)
check("超界实测仍可登记", r.status_code == 201, r.data)
rec2 = r.get_json()
i2 = rec2["items"][0]
check("峰温超界被标出", i2["peak_out_of_range"] is True and i2["actual_peak_temp"] == 1245)
check("保温超界被标出", i2["hold_out_of_range"] is True and i2["actual_hold_minutes"] == 20)
check("汇总有 2 项超界", rec2["all_in_range"] is False and rec2["out_of_range_count"] == 2)

# 只超其中一项
r = reg(s3["id"], 1250, 99, s3)   # BD: 峰[1200,1300]内，保温[10,60]外
check("单项超界登记", r.status_code == 201)
i3 = r.get_json()["items"][0]
check("只标保温超界",
      i3["peak_out_of_range"] is False and i3["hold_out_of_range"] is True)

# ---------- 6. 判定按确认清单快照：后续修改批次范围不改变判定 ----------
bc_now = next(x for x in c.get("/api/batches").get_json() if x["code"] == "BC")
r = c.put(f"/api/batches/{bc_now['id']}", json={
    "peak_min": 1240, "peak_max": 1260, "hold_min": 5, "hold_max": 20,
    "version": bc_now["version"]})
check("批次范围后续仍可修改", r.status_code == 200)
g2 = c.get(f"/api/firing-records/{rec2['id']}").get_json()
check("判定仍按确认快照（峰1245仍超界）",
      g2["items"][0]["peak_min"] == 1230 and g2["items"][0]["peak_max"] == 1240
      and g2["items"][0]["peak_out_of_range"] is True
      and g2["items"][0]["hold_out_of_range"] is True, g2["items"][0])

# ---------- 7. 每个安排只能登记一次 ----------
r = reg(s1["id"], 1230, 30, s1_now)
check("重复登记被拒", r.status_code == 409 and "只能登记一次" in r.get_json()["error"])
check("失败不留部分记录（实绩总数不变）",
      len(c.get("/api/firing-records").get_json()) == 3)

# ---------- 8. 成功登记后安排不得修改 ----------
r = c.put(f"/api/schedules/{s1['id']}", json={
    "start_at": "2026-10-11T08:00:00+08:00", "end_at": "2026-10-11T12:00:00+08:00",
    "peak_temp": 1230, "hold_minutes": 32})
check("已登记安排不可修改", r.status_code == 409 and r.get_json()["locked"] is True)
cur = next(x for x in c.get("/api/schedules").get_json() if x["id"] == s1["id"])
check("拒绝修改后原安排不变",
      cur["start_at"] == s1_now["start_at"] and cur["peak_temp"] == 1230)

# 未登记实绩的安排仍可修改（回归原有排程编辑）
be = add_batch("BE", 1180, 1290, 15, 55)
load4 = confirm_load("L4", k2, [(be, 1)])
s4 = make_schedule(load4["id"],
                   "2026-10-12T08:00:00+08:00", "2026-10-12T12:00:00+08:00",
                   1250, 40)
r = c.put(f"/api/schedules/{s4['id']}", json={
    "start_at": "2026-10-12T09:00:00+08:00", "end_at": "2026-10-12T13:00:00+08:00",
    "peak_temp": 1260, "hold_minutes": 45})
check("未登记实绩的安排仍可修改", r.status_code == 200, r.data)

# 安排列表带 record_id
lst = {x["id"]: x["record_id"] for x in c.get("/api/schedules").get_json()}
check("安排列表反映登记状态",
      lst[s1["id"]] == rec1["id"] and lst[s2["id"]] == rec2["id"]
      and lst[s4["id"]] is None, lst)

# ---------- 9. 重启后仍可查询，装载确认等原有功能不变 ----------
app2 = create_app()
c2 = app2.test_client()
recs = c2.get("/api/firing-records").get_json()
check("重启后实绩保留", len(recs) == 3)
g = c2.get(f"/api/firing-records/{rec2['id']}").get_json()
check("重启后逐批判定可查",
      g["items"][0]["peak_out_of_range"] and g["out_of_range_count"] == 2)
# 装窑确认仍可用（新增表不影响既有流程）
bf = c2.post("/api/batches", json={
    "code": "BF", "peak_min": 1200, "peak_max": 1260,
    "hold_min": 20, "hold_max": 40})
check("重启后批次登记仍可用", bf.status_code == 201)

# ---------- 10. 并发：同一安排两次登记只有一个成功 ----------
results = {}
barrier = []
def race(key):
    client = app.test_client()
    sched = next(x for x in client.get("/api/schedules").get_json() if x["id"] == s4["id"])
    barrier.append(1)
    while len(barrier) < 2:
        pass
    r = client.post(f"/api/schedules/{s4['id']}/firing-record", json={
        "actual_peak_temp": 1255, "actual_hold_minutes": 42,
        "schedule_start_at": sched["start_at"], "schedule_end_at": sched["end_at"],
        "schedule_peak_temp": sched["peak_temp"],
        "schedule_hold_minutes": sched["hold_minutes"]})
    results[key] = (r.status_code, r.get_json())

t1 = Thread(target=race, args=("a",))
t2 = Thread(target=race, args=("b",))
t1.start(); t2.start(); t1.join(); t2.join()
codes = sorted(results[k][0] for k in results)
print("a:", results["a"][0], results["a"][1].get("error", f"实绩 #{results['a'][1].get('id')}"))
print("b:", results["b"][0], results["b"][1].get("error", f"实绩 #{results['b'][1].get('id')}"))
check("并发登记恰好一个成功", codes == [201, 409], results)
check("败方不留部分记录（实绩总数恰为 4）",
      len(c.get("/api/firing-records").get_json()) == 4)

# ---------- 11. 交错：登记与排程修改并发，以数据库最新状态裁决 ----------
bg = add_batch("BG", 1180, 1290, 15, 55)
load5 = confirm_load("L5", k2, [(bg, 1)])
s5 = make_schedule(load5["id"],
                   "2026-10-13T08:00:00+08:00", "2026-10-13T12:00:00+08:00",
                   1250, 40)
order = []
def do_register():
    client = app.test_client()
    order.append("reg-start")
    r = client.post(f"/api/schedules/{s5['id']}/firing-record", json={
        "actual_peak_temp": 1248, "actual_hold_minutes": 38,
        "schedule_start_at": s5["start_at"], "schedule_end_at": s5["end_at"],
        "schedule_peak_temp": 1250, "schedule_hold_minutes": 40})
    order.append(("reg", r.status_code))

def do_update():
    client = app.test_client()
    r = client.put(f"/api/schedules/{s5['id']}", json={
        "start_at": "2026-10-13T09:00:00+08:00", "end_at": "2026-10-13T13:00:00+08:00",
        "peak_temp": 1255, "hold_minutes": 42})
    order.append(("upd", r.status_code))

tr = Thread(target=do_register)
tr.start()
# 让登记线程先进入事务并完成，再串行验证两种裁决结果都自洽：
tr.join()
do_update()
reg_code = next(v for item in order if isinstance(item, tuple) and item[0] == "reg"
                for v in [item[1]])
upd_code = next(v for item in order if isinstance(item, tuple) and item[0] == "upd"
                for v in [item[1]])
check("登记先成功则修改被冻结", reg_code == 201 and upd_code == 409, order)
check("实绩只登记一次（无部分记录）",
      len(c.get("/api/firing-records").get_json()) == 5)

# 反向交错：先修改成功（未登记），再用旧快照登记必须 stale
bh = add_batch("BH", 1180, 1290, 15, 55)
load6 = confirm_load("L6", k2, [(bh, 1)])
s6 = make_schedule(load6["id"],
                   "2026-10-14T08:00:00+08:00", "2026-10-14T12:00:00+08:00",
                   1250, 40)
r = c.put(f"/api/schedules/{s6['id']}", json={
    "start_at": "2026-10-14T10:00:00+08:00", "end_at": "2026-10-14T14:00:00+08:00",
    "peak_temp": 1250, "hold_minutes": 40})
assert r.status_code == 200
r = reg(s6["id"], 1250, 40, s6)
check("先改后登记：旧快照 stale 被拒", r.status_code == 409 and r.get_json()["stale"])
s6_now = next(x for x in c.get("/api/schedules").get_json() if x["id"] == s6["id"])
r = reg(s6["id"], 1250, 40, s6_now)
check("刷新后用新快照可登记", r.status_code == 201, r.data)

print(f"\n✅ 全部 {len(passed)} 项检查通过")
for n in passed:
    print("  -", n)
