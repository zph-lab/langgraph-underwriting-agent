/* 临时登录系统（2026-09-30）
 * 账号密码见 rule_editor/config.yaml 的 login 段（enabled=false 则免登录）。
 * 页面加载先查 /api/auth_check：未登录弹全屏遮罩；登录成功整页刷新，
 * 让 app.js 以带会话 Cookie 的状态重新拉数据。任何 API 返回 401
 * （如服务重启导致内存会话失效）也会重新弹出遮罩。
 */
(function () {
  var mask = document.getElementById("loginMask");
  var user = document.getElementById("loginUser");
  var pass = document.getElementById("loginPass");
  var btn = document.getElementById("loginBtn");
  var err = document.getElementById("loginErr");
  if (!mask || !user || !pass || !btn || !err) return;

  function show() {
    mask.style.display = "flex";
    setTimeout(function () { user.focus(); }, 50);
  }
  function hide() { mask.style.display = "none"; }
  function isOpen() { return mask.style.display === "flex"; }

  function submit() {
    err.textContent = "";
    btn.disabled = true;
    fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: user.value.trim(), password: pass.value })
    }).then(function (r) { return r.json(); }).then(function (d) {
      btn.disabled = false;
      if (d && d.ok) { location.reload(); return; }
      err.textContent = (d && d.error) || "登录失败";
      pass.value = "";
      pass.focus();
    }).catch(function () {
      btn.disabled = false;
      err.textContent = "无法连接服务，请稍后重试";
    });
  }

  btn.addEventListener("click", submit);
  [user, pass].forEach(function (el) {
    el.addEventListener("keydown", function (e) { if (e.key === "Enter") submit(); });
  });

  // 全局 401 兜底：会话过期自动回到登录态
  var rawFetch = window.fetch;
  window.fetch = function () {
    return rawFetch.apply(this, arguments).then(function (res) {
      if (res.status === 401 && !isOpen()) show();
      return res;
    });
  };

  fetch("/api/auth_check").then(function (r) { return r.json(); })
    .then(function (d) { if (!d || !d.authenticated) show(); })
    .catch(function () { /* 服务不可达时不拦页面 */ });
})();
