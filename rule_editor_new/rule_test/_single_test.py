# -*- coding: utf-8 -*-
"""临时脚本：按 gen_test_input_v2.build_hit_input 口径构造 input → ruleSingleTest 验证。"""
import json
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = r"d:\deskface\艾融科技项目文件\intelligent_Rule_Editor"
sys.path.insert(0, _ROOT)
sys.path.insert(0, _ROOT + r"\desc_to_rulecontent")
from write_rule_to_adp import login, DEFAULT_USER, DEFAULT_PASSWORD  # noqa: E402
from gen_test_input_v2 import build_hit_input  # noqa: E402

BASE = "http://192.168.0.205:8902"
RULE_ID = "HHZ2026941103"


def post(token, body):
    req = urllib.request.Request(
        BASE + "/test/ruleSingleTest",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"adp-token": token, "Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return "HTTP%s %s" % (e.code, e.read().decode("utf-8", "replace")[:150])
    resp = j.get("resp") or {}
    return "pass=%s errMsg=%r" % (resp.get("pass"), j.get("errMsg"))


def main():
    token = login(BASE, DEFAULT_USER, DEFAULT_PASSWORD)
    # 1) 取 ruleContent
    r = urllib.request.Request(BASE + "/decision/rule/" + RULE_ID,
                               headers={"adp-token": token, "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(r, timeout=30) as resp:
        j = json.loads(resp.read().decode("utf-8", "replace"))
    rc = json.loads(j["resp"]["ruleContent"])

    # 2) v2 口径构造 input
    inp = build_hit_input(rc)
    print("=== build_hit_input 生成的 input ===")
    print(json.dumps(inp, ensure_ascii=False, indent=1))

    # 3) 变体提交
    import copy
    inp75 = copy.deepcopy(inp)
    try:
        inp75["appbase"]["productLifeInfo06"]["coverage062008List"][0]["nfloatrate"] = 75
    except Exception:
        pass

    variants = [
        ("A v2口径 app=Actuator_New_App flag=F", inp, "Actuator_New_App", False),
        ("B v2口径 app=test flag=F",             inp, "test", False),
        ("C v2口径 app=Actuator_New_App flag=T", inp, "Actuator_New_App", True),
        ("D 对照 nfloatrate=75 app=Actuator_New_App", inp75, "Actuator_New_App", False),
    ]
    print("=== ruleSingleTest 结果 ===")
    for name, body_inp, app, flag in variants:
        body = {"flag": flag, "applicationId": app, "ruleId": RULE_ID, "input": body_inp}
        print("---", name)
        try:
            print("   ", post(token, body))
        except Exception as e:
            print("    EXC:", e)
        time.sleep(0.2)


if __name__ == "__main__":
    main()
