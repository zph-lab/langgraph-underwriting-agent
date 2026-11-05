/* 顶部按钮通过原生链接跳转；兼容旧助手 hash 地址。 */
(() => {
  if (location.hash === '#rule-ai-assistant') location.replace('/assistant/' + location.search);
})();
