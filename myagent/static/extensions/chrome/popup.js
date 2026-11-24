const show = async () => { const { code, status } = await chrome.storage.local.get(["code", "status"]);
  document.getElementById("code").value = code || "";
  const st = document.getElementById("st"); st.textContent = status || (code ? "Connecting…" : "Not paired yet");
  st.className = status === "Connected" ? "ok" : status ? "bad" : ""; };
document.getElementById("save").onclick = async () => {
  await chrome.storage.local.set({ code: document.getElementById("code").value.trim(), status: "Connecting…" }); show(); setTimeout(show, 1500); };
show(); setInterval(show, 1500);
