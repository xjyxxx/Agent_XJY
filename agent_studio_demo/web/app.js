/* Persona Replay 前端（原生 JS，无构建步骤）
 *
 * 设计取舍：
 * - 用 fetch + ReadableStream 读 SSE，而不是 EventSource：EventSource 会自动重连，
 *   重连时会重放请求，等于重复发消息；fetch 可以精确控制中止。
 * - 所有状态都在一个 `state` 对象里，渲染函数只读它，方便排查。
 * - 事件流面板原样展示 SSE 事件：这个项目的卖点是"可解释"，UI 就不该把它藏起来。
 */

const state = {
  sessionId: null,
  chatPath: null,
  speakers: [],
  userSpeaker: null,
  targetSpeaker: null,
  streaming: false,
  assistantEl: null,
  messageCount: 0,
  lastSent: null,
};

const STREAM_TIMEOUT_MS = 120000;
const STREAM_MAX_RETRIES = 2;

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

function log(event, data) {
  const logEl = $("event-log");
  const time = new Date().toLocaleTimeString();
  const line = `[${time}] ${event}\n${JSON.stringify(data)}\n\n`;
  logEl.textContent += line;
  logEl.scrollTop = logEl.scrollHeight;
}

function setHint(id, text, isError = false) {
  const node = $(id);
  node.textContent = text;
  node.className = "hint" + (isError ? " error" : "");
}

async function api(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      ...options,
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || body);
      throw new Error(`${response.status} ${detail}`);
    }
    return body;
  } finally {
    clearTimeout(timer);
  }
}

/* ---------------- 错误横幅 + 重试策略 ---------------- */
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function showBanner(text, { retryable = false, info = false } = {}) {
  $("banner-text").textContent = text;
  $("banner").classList.toggle("info", info);
  $("banner-retry").classList.toggle("hidden", !retryable);
  $("banner").classList.remove("hidden");
}

function hideBanner() {
  $("banner").classList.add("hidden");
  $("banner-retry").classList.add("hidden");
}

function isTransient(error) {
  if (!error) return false;
  if (error.name === "AbortError") return true; // 超时
  if (error instanceof TypeError) return true; // fetch 网络层失败
  return /network|fetch|aborted|timeout|连接|断开/i.test(String(error.message || error));
}

/* ---------------- 1. 健康检查 ---------------- */
async function checkHealth() {
  const pill = $("health");
  try {
    const data = await api("/health");
    pill.textContent = `服务正常 · ${data.model || "?"}${data.stream ? " · 流式" : ""}`;
    pill.className = "pill ok";
  } catch (error) {
    pill.textContent = "服务不可用";
    pill.className = "pill bad";
  }
}

/* ---------------- 2. 导入 ---------------- */
async function importChat() {
  const path = $("chat-path").value.trim();
  if (!path) return setHint("import-result", "请填写路径", true);
  setHint("import-result", "解析中…");
  try {
    const data = await api("/chats/import", {
      method: "POST",
      body: JSON.stringify({ path }),
    });
    state.chatPath = path;
    state.speakers = data.speakers;
    renderSpeakerOptions(data.speakers, data.user_speaker, data.target_speaker);
    setHint(
      "import-result",
      `识别到 ${data.speakers.join(" / ")}，共 ${data.message_count} 条消息；chat_id=${data.chat_id}`
    );
    $("session-section").classList.remove("hidden");
    renderPersona(data.persona);
  } catch (error) {
    setHint("import-result", `导入失败：${error.message}`, true);
  }
}

function renderSpeakerOptions(speakers, user, target) {
  const userSelect = $("user-speaker");
  const targetSelect = $("target-speaker");
  userSelect.innerHTML = "";
  targetSelect.innerHTML = "";
  speakers.forEach((name, index) => {
    userSelect.append(new Option(name, name, index === 0, index === 0));
    targetSelect.append(new Option(name, name, index === 1, index === 1));
  });
  if (user) userSelect.value = user;
  if (target) targetSelect.value = target;
}

/* ---------------- 3. 建会话 ---------------- */
async function createSession() {
  const userSpeaker = $("user-speaker").value;
  const targetSpeaker = $("target-speaker").value;
  if (userSpeaker === targetSpeaker) {
    return setHint("session-result", "「我是」和「AI 扮演」不能是同一个人", true);
  }
  setHint("session-result", "建立会话中…");
  try {
    const data = await api("/sessions", {
      method: "POST",
      body: JSON.stringify({
        path: state.chatPath,
        user_speaker: userSpeaker,
        target_speaker: targetSpeaker,
      }),
    });
    state.sessionId = data.session_id;
    state.userSpeaker = data.user_speaker;
    state.targetSpeaker = data.target_speaker;
    setHint("session-result", `会话 ${data.session_id} 就绪：你=${data.user_speaker}，AI=${data.target_speaker}`);
    $("session-pill").textContent = `会话 ${data.session_id}`;
    $("session-pill").className = "pill ok";
    $("chat-section").classList.remove("hidden");
    $("messages").innerHTML = "";
    state.messageCount = 0;
    renderPersona(data.persona);
    log("session", { session_id: data.session_id, persona: data.persona });
  } catch (error) {
    setHint("session-result", `建立失败：${error.message}`, true);
  }
}

/* ---------------- 4. 渲染 ---------------- */
function renderPersona(persona) {
  const card = $("persona-card");
  card.innerHTML = "";
  if (!persona) return;
  card.append(el("div", null, `人设卡 · ${persona.speaker}`));
  const chips = el("div", "chips");
  (persona.common_terms || []).slice(0, 6).forEach((term) => chips.append(el("span", "chip", term)));
  if (persona.average_message_length) {
    chips.append(el("span", "chip", `平均 ${persona.average_message_length} 字`));
  }
  chipList(card, "口头禅", persona.common_terms);
  chipList(card, "标点", persona.punctuation_style);
  card.append(chips);
  if ((persona.example_replies || []).length) {
    card.append(el("div", null, `示例：${persona.example_replies.join(" / ")}`));
  }
}

function chipList(parent, label, values) {
  if (!values || !values.length) return;
  parent.append(el("div", null, `${label}：${values.join(" ")}`));
}

function renderEvidence(hits) {
  const list = $("evidence-list");
  list.innerHTML = "";
  if (!hits || !hits.length) {
    list.append(el("div", "card", "这一轮没有达到阈值的依据（模型只能靠人设说话，不应编造事实）"));
    return;
  }
  hits.forEach((hit) => {
    const item = el("div", "ev-item");
    const head = el("div", "ev-head");
    head.append(el("span", null, `${hit.speaker} · 第 ${hit.line_number} 行`));
    head.append(el("span", "score", hit.score.toFixed(3)));
    item.append(head);
    item.append(el("div", "ev-content", hit.content));
    if (hit.context) item.append(el("div", "ev-context", `上文：${hit.context}`));
    list.append(item);
  });
}

function renderCritique(critique) {
  const box = $("critique-box");
  box.innerHTML = "";
  if (!critique) {
    box.textContent = "还没有判定结果。";
    return;
  }
  const verdict = critique.verdict || "?";
  const head = el("div");
  head.append(el("span", "verdict " + verdict, verdict === "pass" ? "通过" : verdict === "review" ? "需要人工" : verdict));
  if (critique.human) head.append(el("span", "tag", `人工：${critique.human}`));
  box.append(head);
  const problems = critique.problems || [];
  if (problems.length) {
    const wrap = el("div", "problems");
    problems.forEach((problem) => wrap.append(el("span", "problem", problem)));
    box.append(wrap);
  } else {
    box.append(el("div", "hint", "未发现助手腔、串台、超长或事实问题"));
  }
}

function addMessage(role, text) {
  const wrap = el("div", "msg " + (role === "me" ? "me" : "ai"));
  wrap.append(el("div", "who", role === "me" ? "我" : (state.targetSpeaker || "AI")));
  const bubble = el("div", "bubble", text || "");
  wrap.append(bubble);
  $("messages").append(wrap);
  $("messages").scrollTop = $("messages").scrollHeight;
  return bubble;
}

/* ---------------- 5. 发送 + SSE ----------------
 * 重试边界（重要）：
 * - 只能在"还没吐出任何内容"时自动重试。已经开始输出后重试 = 两段文本拼接 +
 *   同一轮重复计费，比失败更糟。
 * - 自动重试只覆盖网络/超时；4xx/5xx 的 JSON 错误不重试（那是确定性的失败）。
 * - 已经收到部分内容时，横幅改为提示"点重试会重发这条消息"——因为 SSE 是
 *   一次性 POST 流，服务端没有 resumable 语义，只能整轮重发。
 */
async function sendMessage(text, { retry = false } = {}) {
  if (!state.sessionId || state.streaming || !text.trim()) return;
  state.streaming = true;
  state.lastSent = text;
  $("message").value = "";
  if (!retry) addMessage("me", text);
  const bubble = state.assistantEl || addMessage("ai", "");
  bubble.textContent = "";
  bubble.parentElement.classList.add("pending");
  state.assistantEl = bubble;
  $("evidence-list").innerHTML = "";
  renderCritique(null);

  const url = `/sessions/${state.sessionId}/stream?message=${encodeURIComponent(text)}`;
  let attempt = 0;
  try {
    for (;;) {
      attempt += 1;
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), STREAM_TIMEOUT_MS);
      try {
        const response = await fetch(url, { signal: controller.signal });
        if (!response.ok || !response.body) {
          const body = await response.json().catch(() => ({}));
          throw new Error(`${response.status} ${body.detail || ""}`.trim());
        }
        await readSSE(response.body, handleEvent);
        hideBanner();
        state.messageCount += 1;
        return;
      } catch (error) {
        const partial = (bubble.textContent || "").length > 0;
        if (!partial && attempt <= STREAM_MAX_RETRIES && isTransient(error)) {
          log("retry", { attempt, message: error.message });
          await sleep(500 * Math.pow(3, attempt - 1));
          continue;
        }
        throw error;
      } finally {
        clearTimeout(timer);
      }
    }
  } catch (error) {
    const partial = (bubble.textContent || "").length > 0;
    bubble.parentElement.classList.remove("pending");
    if (!bubble.textContent) bubble.textContent = "（没有收到回复）";
    showBanner(
      partial
        ? `连接中断：已保留收到的内容。点「重试」会重新发送这条消息（可能多算一轮）。`
        : `请求失败：${error.message}`,
      { retryable: Boolean(state.lastSent) }
    );
    log("error", { message: error.message, partial, attempts: attempt });
  } finally {
    state.streaming = false;
    bubble.parentElement.classList.remove("pending");
  }
}

async function readSSE(stream, onEvent) {
  const reader = stream.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let index;
    while ((index = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, index);
      buffer = buffer.slice(index + 2);
      parseFrame(frame, onEvent);
    }
  }
}

function parseFrame(frame, onEvent) {
  let event = "message";
  const dataLines = [];
  frame.split(/\r?\n/).forEach((line) => {
    if (line.startsWith("event: ")) event = line.slice(7).trim();
    else if (line.startsWith("data: ")) dataLines.push(line.slice(6));
  });
  if (!dataLines.length) return;
  let data = {};
  try {
    data = JSON.parse(dataLines.join("\n"));
  } catch (error) {
    data = { raw: dataLines.join("\n") };
  }
  onEvent(event, data);
}

function handleEvent(event, data) {
  log(event, data);
  switch (event) {
    case "persona":
      renderPersona(data.persona);
      break;
    case "retrieved":
      renderEvidence(data.hits);
      break;
    case "draft":
      if (state.assistantEl && !state.assistantEl.textContent) {
        state.assistantEl.textContent = data.draft || "";
      }
      break;
    case "critique":
      renderCritique(data.critique);
      break;
    case "reply_delta":
      state.assistantEl.textContent += data.delta || "";
      $("messages").scrollTop = $("messages").scrollHeight;
      break;
    case "reply_reset":
      state.assistantEl.textContent = "";
      break;
    case "interrupt":
      showInterrupt(data);
      break;
    case "done":
      if (data.status === "interrupted") {
        // 正文未经批准，不留在气泡里
        state.assistantEl.textContent = "";
        state.assistantEl.parentElement.classList.remove("pending");
      } else if (data.reply) {
        state.assistantEl.textContent = data.reply;
      }
      break;
    case "error":
      state.assistantEl.textContent = `（流错误：${data.detail || ""}）`;
      showBanner(`服务端流错误：${data.detail || "未知原因"}`, { retryable: true });
      break;
    default:
      break;
  }
}

/* ---------------- 6. HITL ---------------- */
function showInterrupt(pending) {
  $("interrupt-box").classList.remove("hidden");
  $("interrupt-reason").textContent =
    `原因：${pending.reason || "?"}｜问题：${(pending.problems || []).join("、") || "无"}`;
  $("interrupt-reply").textContent = pending.reply || "";
  $("interrupt-edit").value = "";
}

async function decide(action) {
  if (!state.sessionId) return;
  const reply = $("interrupt-edit").value.trim();
  if (action === "edit" && !reply) {
    return setHint("interrupt-reason", "选择「按改写内容发送」时必须填写改写内容", true);
  }
  try {
    const data = await api(`/sessions/${state.sessionId}/resume`, {
      method: "POST",
      body: JSON.stringify({ action, reply }),
    });
    $("interrupt-box").classList.add("hidden");
    renderCritique(data.critique);
    if (data.reply) state.assistantEl.textContent = data.reply;
    state.assistantEl.parentElement.classList.remove("pending");
    log("resume", { action, reply: data.reply, status: data.status });
  } catch (error) {
    $("interrupt-reason").textContent = `裁决失败：${error.message}`;
  }
}

/* ---------------- 7. 绑定 ---------------- */
$("btn-import").addEventListener("click", importChat);
$("btn-session").addEventListener("click", createSession);
$("banner-close").addEventListener("click", hideBanner);
$("banner-retry").addEventListener("click", () => {
  hideBanner();
  if (state.lastSent) sendMessage(state.lastSent, { retry: true });
});
window.addEventListener("offline", () =>
  showBanner("网络已断开；恢复后点「重试」可继续。", { retryable: true, info: true })
);
window.addEventListener("online", () => {
  hideBanner();
  checkHealth();
});
$("composer").addEventListener("submit", (event) => {
  event.preventDefault();
  sendMessage($("message").value);
});
document.querySelectorAll("[data-decision]").forEach((button) => {
  button.addEventListener("click", () => decide(button.dataset.decision));
});
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
    tab.classList.add("active");
    ["evidence", "critique", "events"].forEach((name) => {
      $(`tab-${name}`).classList.toggle("hidden", name !== tab.dataset.tab);
    });
  });
});

checkHealth();
setInterval(checkHealth, 30000);
