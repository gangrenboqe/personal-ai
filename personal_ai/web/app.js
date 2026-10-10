(() => {
  const canvas = document.getElementById("particleCanvas");
  const ctx = canvas.getContext("2d");

  const state = {
    mode: "now",
    activity: "idle",
    queue: [],
    dpr: Math.min(window.devicePixelRatio || 1, 2),
    width: 0,
    height: 0,
    time: 0,
    last: performance.now(),
    particles: [],
    mouse: { x: -9999, y: -9999, active: false, down: false }
  };

  const ui = {
    stateEyebrow: document.getElementById("stateEyebrow"),
    mainTitle: document.getElementById("mainTitle"),
    mainDescription: document.getElementById("mainDescription"),
    visualState: document.getElementById("visualState"),
    visualHint: document.getElementById("visualHint"),
    workValue: document.getElementById("workValue"),
    queueText: document.getElementById("queueText"),
    queuedCount: document.getElementById("queuedCount"),
    queuePanel: document.getElementById("queuePanel"),
    queueList: document.getElementById("queueList"),
    panelState: document.getElementById("panelState"),
    panelTask: document.getElementById("panelTask"),
    panelQueue: document.getElementById("panelQueue"),
    messageInput: document.getElementById("messageInput"),
    systemPanel: document.getElementById("systemPanel"),
    panelBackdrop: document.getElementById("panelBackdrop"),
    statusDot: document.getElementById("statusDot"),
    sessionSelect: document.getElementById("sessionSelect"),
    newSessionButton: document.getElementById("newSessionButton"),
    chatLog: document.getElementById("chatLog"),
    chatChain: document.getElementById("chatChain"),
    panelEngine: document.getElementById("panelEngine"),
    panelGateway: document.getElementById("panelGateway"),
    panelProvider: document.getElementById("panelProvider"),
    panelModel: document.getElementById("panelModel"),
    panelUsage: document.getElementById("panelUsage"),
    chatPanel: document.getElementById("chatPanel"),
    futurePanel: document.getElementById("futurePanel"),
    taskInput: document.getElementById("taskInput"),
    taskList: document.getElementById("taskList"),
    taskCount: document.getElementById("taskCount"),
  };

  // ---------------------------------------------------------------
  // 后端 API：A3 之后这里全部是真实数据，不再有模拟回答
  // ---------------------------------------------------------------

  const backend = { sessionId: "", ready: false };

  async function apiGet(path) {
    const res = await fetch(path, { headers: { Accept: "application/json" } });
    if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
    return res.json();
  }

  async function apiPost(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `${path} -> HTTP ${res.status}`);
    return data;
  }

  async function apiSend(method, path, body) {
    const res = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `${path} -> HTTP ${res.status}`);
    return data;
  }

  const apiPatch = (path, body) => apiSend("PATCH", path, body);
  const apiDelete = (path) => apiSend("DELETE", path);

  async function loadHealth() {
    try {
      const h = await apiGet("/api/health");
      const p = h.provider || {};
      ui.panelEngine.textContent = p.name || "—";
      ui.panelGateway.textContent = (p.chain || []).join(" → ") || "—";
      ui.panelProvider.textContent = h.config?.base_url || "本地兜底";
      ui.panelModel.textContent = p.model || "—";
      ui.panelUsage.textContent = "SQLite 已启用";
      ui.chatChain.textContent = (p.chain || []).join(" → ") || "—";
      backend.ready = true;
    } catch (err) {
      ui.panelEngine.textContent = "后端未连接";
      ui.panelGateway.textContent = String(err.message || err);
      ui.chatChain.textContent = "后端未连接";
      backend.ready = false;
    }
  }

  async function loadSessions() {
    try {
      const list = await apiGet("/api/sessions");
      ui.sessionSelect.innerHTML = "";
      list.forEach((s) => {
        const opt = document.createElement("option");
        opt.value = s.id;
        opt.textContent = `${s.title || "未命名"} · ${s.msg_count} 条`;
        ui.sessionSelect.appendChild(opt);
      });
      if (!backend.sessionId && list.length) {
        backend.sessionId = list[0].id;
        ui.sessionSelect.value = backend.sessionId;
        await loadHistory();
      }
    } catch (err) {
      ui.sessionSelect.innerHTML = "";
    }
  }

  async function loadHistory() {
    const sid = backend.sessionId;
    if (!sid) return;
    const msgs = await apiGet(`/api/sessions/${sid}/messages`);
    ui.chatLog.innerHTML = "";
    if (!msgs.length) {
      ui.chatLog.innerHTML = '<div class="chat-empty">还没有对话。发一条消息，它会落进你自己的 SQLite。</div>';
      return;
    }
    msgs.forEach((m) => appendBubble(m.role, m.content, m.chain));
    ui.chatLog.scrollTop = ui.chatLog.scrollHeight;
  }

  function appendBubble(role, content, chain) {
    const empty = ui.chatLog.querySelector(".chat-empty");
    if (empty) empty.remove();

    const row = document.createElement("div");
    row.className = `chat-row ${role}`;

    const bubble = document.createElement("div");
    bubble.className = "chat-bubble";
    bubble.textContent = content;

    const meta = document.createElement("div");
    meta.className = "chat-meta";
    meta.textContent = role === "user" ? "我" : (chain || "assistant");

    row.appendChild(meta);
    row.appendChild(bubble);
    ui.chatLog.appendChild(row);
    ui.chatLog.scrollTop = ui.chatLog.scrollHeight;
  }

  // ---------------------------------------------------------------
  // 未来区：任务（真实数据，来自 /api/tasks）
  // ---------------------------------------------------------------

  const STATUS_LABEL = { todo: "待开始", doing: "进行中", done: "已完成" };

  async function loadTasks() {
    try {
      const tasks = await apiGet("/api/tasks");
      renderTasks(tasks);
    } catch (err) {
      ui.taskList.innerHTML = `<div class="chat-empty">任务加载失败：${err.message}</div>`;
    }
  }

  function renderTasks(tasks) {
    ui.taskCount.textContent = `${tasks.length} 条`;
    ui.taskList.innerHTML = "";
    if (!tasks.length) {
      ui.taskList.innerHTML = '<div class="chat-empty">还没有任务。加一条，它就属于「未来」。</div>';
      return;
    }
    tasks.forEach((t) => {
      const row = document.createElement("div");
      row.className = `task-row ${t.status}`;

      const box = document.createElement("div");
      box.className = "task-main";

      const title = document.createElement("div");
      title.className = "task-title";
      title.textContent = t.title;

      const meta = document.createElement("div");
      meta.className = "task-meta";
      meta.textContent = STATUS_LABEL[t.status] || t.status;

      box.appendChild(title);
      box.appendChild(meta);

      const actions = document.createElement("div");
      actions.className = "task-actions";

      const next = t.status === "todo" ? "doing" : (t.status === "doing" ? "done" : "todo");
      const advance = document.createElement("button");
      advance.className = "subtle-button";
      advance.textContent = t.status === "done" ? "重开" : "推进";
      advance.addEventListener("click", async () => {
        await apiPatch(`/api/tasks/${t.id}`, { status: next });
        loadTasks();
      });

      const del = document.createElement("button");
      del.className = "subtle-button";
      del.textContent = "删除";
      del.addEventListener("click", async () => {
        await apiDelete(`/api/tasks/${t.id}`);
        loadTasks();
      });

      actions.appendChild(advance);
      actions.appendChild(del);
      row.appendChild(box);
      row.appendChild(actions);
      ui.taskList.appendChild(row);
    });
  }

  async function addTask() {
    const title = ui.taskInput.value.trim();
    if (!title) return;
    ui.taskInput.value = "";
    try {
      await apiPost("/api/tasks", { title });
      loadTasks();
    } catch (err) {
      ui.taskList.innerHTML = `<div class="chat-empty">添加失败：${err.message}</div>`;
    }
  }

  const copy = {
    past: {
      eyebrow: "PAST · 已发生",
      title: "让过去真正成为现在的一部分。",
      desc: "历史、项目与经历以后会在这里沉淀。它们不是一个档案柜，而是仍然影响今天的背景。",
      state: "已记录",
      hint: "等待历史数据进入",
      work: "历史与经历"
    },
    now: {
      eyebrow: "NOW · 当前发生",
      title: "让 AI 的行动变得可见。",
      desc: "现在是共同工作的地方。对话、Agent、工具、状态都将在这里发生，而时间轴始终保持存在。",
      state: "空闲",
      hint: "等待下一刻",
      work: "自由思考"
    },
    future: {
      eyebrow: "FUTURE · 将发生",
      title: "让尚未发生的事情拥有位置。",
      desc: "持续任务、计划与未来能力以后会在这里苏醒。它不是待办清单，而是正在形成的可能。",
      state: "等待",
      hint: "暂无运行中的任务",
      work: "未来事件"
    }
  };

  function resize() {
    state.width = window.innerWidth;
    state.height = window.innerHeight;
    canvas.width = Math.floor(state.width * state.dpr);
    canvas.height = Math.floor(state.height * state.dpr);
    canvas.style.width = `${state.width}px`;
    canvas.style.height = `${state.height}px`;
    ctx.setTransform(state.dpr, 0, 0, state.dpr, 0, 0);
  }

  function random(min, max) {
    return min + Math.random() * (max - min);
  }

  function initParticles() {
    const count = Math.min(1700, Math.max(720, Math.floor((state.width * state.height) / 1100)));
    state.particles = Array.from({ length: count }, () => ({
      x: Math.random() * state.width,
      y: Math.random() * state.height,
      vx: random(-0.08, 0.08),
      vy: random(-0.06, 0.06),
      base: Math.random(),
      phase: Math.random() * Math.PI * 2,
      size: random(0.35, 1.45),
      depth: random(0.35, 1.0)
    }));
  }

  function activityFactor() {
    if (state.activity === "running") return 1;
    if (state.activity === "queued") return 0.78;
    if (state.activity === "complete") return 0.45;
    return 0.24;
  }

  function drawParticles(dt) {
    ctx.clearRect(0, 0, state.width, state.height);

    const t = state.time;
    const cx = state.width * 0.5;
    const cy = state.height * 0.49;
    const factor = activityFactor();

    // Soft ambient field.
    const bg = ctx.createRadialGradient(cx, cy, 0, cx, cy, Math.min(state.width, state.height) * 0.52);
    bg.addColorStop(0, "rgba(156,185,225,0.055)");
    bg.addColorStop(0.45, "rgba(95,118,153,0.018)");
    bg.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, state.width, state.height);

    for (const p of state.particles) {
      // A slow field makes the whole background breathe rather than orbit like a spinner.
      const nx = Math.sin((p.y * 0.0024) + t * 0.18 + p.phase);
      const ny = Math.cos((p.x * 0.0017) - t * 0.16 + p.phase * 0.7);

      p.vx += nx * 0.0008 * factor;
      p.vy += ny * 0.0007 * factor;

      // Mouse interaction: nearby particles are gently pushed and then return to the field.
      if (state.mouse.active) {
        const dx = p.x - state.mouse.x;
        const dy = p.y - state.mouse.y;
        const dist2 = dx * dx + dy * dy;
        const radius = state.mouse.down ? 190 : 135;

        if (dist2 < radius * radius) {
          const dist = Math.sqrt(dist2) || 1;
          const strength = (1 - dist / radius) * (state.mouse.down ? 0.62 : 0.34);
          p.vx += (dx / dist) * strength;
          p.vy += (dy / dist) * strength;

          // Small tangential wave adds the "water / field" feeling.
          p.vx += (-dy / dist) * strength * 0.065;
          p.vy += ( dx / dist) * strength * 0.065;
        }
      }

      // Gentle return to center-biased cloud so the field stays coherent.
      const dx0 = cx - p.x;
      const dy0 = cy - p.y;
      p.vx += dx0 * 0.0000025 * factor;
      p.vy += dy0 * 0.0000017 * factor;

      p.vx *= 0.985;
      p.vy *= 0.985;
      p.x += p.vx * dt * 0.065;
      p.y += p.vy * dt * 0.065;

      // wrap
      if (p.x < -20) p.x = state.width + 20;
      if (p.x > state.width + 20) p.x = -20;
      if (p.y < -20) p.y = state.height + 20;
      if (p.y > state.height + 20) p.y = -20;

      const pulse = 0.7 + 0.3 * Math.sin(t * 1.2 + p.phase);
      const alpha = (0.16 + p.depth * 0.22) * pulse;
      const size = p.size * (0.9 + factor * 0.5);

      ctx.beginPath();
      ctx.arc(p.x, p.y, size, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(214,229,249,${alpha})`;
      ctx.fill();
    }

    // Long, very subtle wave bands.
    ctx.save();
    ctx.lineWidth = 1;
    for (let band = 0; band < 7; band++) {
      ctx.beginPath();
      const amp = 8 + band * 1.5 + factor * 8;
      const speed = 0.33 + band * 0.025;
      for (let x = 0; x <= state.width; x += 9) {
        const y = cy + (band - 3) * 19 +
          Math.sin(x * 0.0062 + t * speed + band * 0.8) * amp +
          Math.sin(x * 0.0026 - t * 0.22 + band) * 12;
        if (x === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.strokeStyle = `rgba(178,204,236,${0.018 + band * 0.003})`;
      ctx.stroke();
    }
    ctx.restore();
  }

  function animate(now) {
    const dt = Math.min(32, now - state.last);
    state.last = now;
    state.time += dt * 0.001;
    drawParticles(dt);
    requestAnimationFrame(animate);
  }

  function setMode(mode) {
    state.mode = mode;
    document.body.dataset.mode = mode;

    document.querySelectorAll(".time-node").forEach(el => {
      el.classList.toggle("active", el.dataset.focus === mode);
    });

    // 未来区只在「未来」出现，聊天区在「过去 / 现在」出现
    ui.chatPanel.hidden = mode === "future";
    ui.futurePanel.hidden = mode !== "future";
    if (mode === "future") loadTasks();

    const data = copy[mode];
    ui.stateEyebrow.textContent = data.eyebrow;
    ui.mainTitle.textContent = data.title;
    ui.mainDescription.textContent = data.desc;
    ui.workValue.textContent = data.work;
    ui.panelTask.textContent = data.work;

    if (state.activity === "idle" || state.activity === "complete") {
      ui.visualState.textContent = data.state;
      ui.visualHint.textContent = data.hint;
      ui.panelState.textContent = data.state;
    }
  }

  function setActivity(activity) {
    state.activity = activity;
    const map = {
      idle: ["空闲", "等待下一刻"],
      running: ["正在工作", "状态正在变化"],
      queued: ["处理中", "已有输入进入队列"],
      complete: ["已完成", "结果已经留下"]
    };

    const [label, hint] = map[activity];
    ui.visualState.textContent = label;
    ui.visualHint.textContent = hint;
    ui.panelState.textContent = label;

    if (activity === "running") ui.statusDot.style.background = "#d9e7ff";
    else if (activity === "queued") ui.statusDot.style.background = "#f1cf8a";
    else ui.statusDot.style.background = "#8de2b3";
  }

  function renderQueue() {
    const count = state.queue.length;
    ui.queuedCount.textContent = `${count} 条待处理`;
    ui.queueText.textContent = count ? `已有 ${count} 条输入等待处理` : "输入队列为空";
    ui.panelQueue.textContent = String(count);
    ui.queuePanel.hidden = count === 0;
    ui.queueList.innerHTML = "";

    state.queue.forEach((text, idx) => {
      const item = document.createElement("div");
      item.className = "queue-item";
      item.textContent = `${idx + 1}. ${text}`;
      ui.queueList.appendChild(item);
    });
  }

  async function sendStream(sid, text) {
    // 流式：用 fetch + ReadableStream 逐块读，边收边显示（不必等全部生成完）
    const res = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sid, message: text })
    });
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    let bubble = null;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE 约定：事件之间用空行分隔
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const raw = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const line = raw.split("\n").find(l => l.startsWith("data:"));
        if (!line) continue;
        const payload = line.slice(5).trim();
        if (payload === "[DONE]") continue;
        let obj;
        try { obj = JSON.parse(payload); } catch { continue; }

        if (obj.error) { throw new Error(obj.error); }

        if (obj.done) {
          backend.sessionId = obj.session_id || sid;
          ui.chatChain.textContent = obj.chain || "—";
          if (bubble) {
            bubble.classList.remove("streaming");
            bubble.setMeta(obj.ok === false ? "生成失败" : (obj.model ? "完成 · " + obj.model : "完成"));
            bubble = null;
          }
          continue;
        }
        if (obj.delta) {
          if (!bubble) bubble = appendStreamingBubble();
          bubble.append(obj.delta);
        }
      }
    }
  }

  function appendStreamingBubble() {
    const empty = ui.chatLog.querySelector(".chat-empty");
    if (empty) empty.remove();
    const row = document.createElement("div");
    row.className = "chat-row assistant";
    const meta = document.createElement("div");
    meta.className = "chat-meta";
    meta.textContent = "生成中…";
    const bubble = document.createElement("div");
    bubble.className = "chat-bubble streaming";
    row.appendChild(meta);
    row.appendChild(bubble);
    ui.chatLog.appendChild(row);
    ui.chatLog.scrollTop = ui.chatLog.scrollHeight;
    bubble.append = function (text) {
      bubble.textContent += text;
      ui.chatLog.scrollTop = ui.chatLog.scrollHeight;
    };
    bubble.setMeta = function (text) {
      meta.textContent = text;
    };
    return bubble;
  }

  async function enqueue(text) {
    state.queue.push(text);
    renderQueue();
    setActivity("queued");

    window.setTimeout(async () => {
      state.queue.shift();
      renderQueue();
      setActivity("running");
      appendBubble("user", text, "");

      try {
        if (!backend.sessionId) {
          const created = await apiPost("/api/sessions", { title: text.slice(0, 20) });
          backend.sessionId = created.session_id;
          await loadSessions();
          ui.sessionSelect.value = backend.sessionId;
        }
        const result = await sendStream(backend.sessionId, text);
        if (result === false) throw new Error("流式响应异常");
        loadSessions();
        loadHealth();
      } catch (err) {
        appendBubble("assistant", `请求失败：${err.message}`, "error");
      }

      window.setTimeout(() => {
        setActivity(state.queue.length ? "queued" : "idle");
        if (!state.queue.length) setMode(state.mode);
      }, 900);
    }, 260);
  }

  function send() {
    const text = ui.messageInput.value.trim();
    if (!text) return;
    ui.messageInput.value = "";
    enqueue(text);
  }

  function openPanel() {
    ui.systemPanel.classList.add("open");
    ui.systemPanel.setAttribute("aria-hidden", "false");
    ui.panelBackdrop.hidden = false;
  }

  function closePanel() {
    ui.systemPanel.classList.remove("open");
    ui.systemPanel.setAttribute("aria-hidden", "true");
    ui.panelBackdrop.hidden = true;
  }

  document.querySelectorAll(".time-node").forEach(btn => {
    btn.addEventListener("click", () => setMode(btn.dataset.focus));
  });

  document.getElementById("sendButton").addEventListener("click", send);

  ui.messageInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && event.ctrlKey) {
      event.preventDefault();
      send();
    }
  });

  document.getElementById("clearQueueButton").addEventListener("click", () => {
    state.queue = [];
    renderQueue();
    setActivity("idle");
    setMode(state.mode);
  });

  ui.newSessionButton.addEventListener("click", async () => {
    try {
      const created = await apiPost("/api/sessions", { title: "新会话" });
      backend.sessionId = created.session_id;
      await loadSessions();
      ui.sessionSelect.value = backend.sessionId;
      ui.chatLog.innerHTML = "";
      appendBubble("assistant", "新会话已创建，说点什么吧。", "");
    } catch (err) {
      appendBubble("assistant", `新建会话失败：${err.message}`, "error");
    }
  });

  ui.taskInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      addTask();
    }
  });

  ui.sessionSelect.addEventListener("change", async () => {
    backend.sessionId = ui.sessionSelect.value;
    await loadHistory();
  });

  document.getElementById("statusButton").addEventListener("click", () => {
    loadHealth();
    openPanel();
  });
  document.getElementById("closeStatusButton").addEventListener("click", closePanel);
  ui.panelBackdrop.addEventListener("click", closePanel);

  document.getElementById("themeButton").addEventListener("click", () => {
    const current = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim();
    const next = current === "rgb(217, 231, 255)" ? "#d7c6ff" : "#d9e7ff";
    document.documentElement.style.setProperty("--accent", next);
    document.documentElement.style.setProperty(
      "--accent-strong",
      next === "#d7c6ff" ? "#efe7ff" : "#edf4ff"
    );
  });

  window.addEventListener("resize", () => {
    resize();
    initParticles();
  });

  canvas.addEventListener("pointermove", (event) => {
    state.mouse.x = event.clientX;
    state.mouse.y = event.clientY;
    state.mouse.active = true;
  });

  canvas.addEventListener("pointerleave", () => {
    state.mouse.active = false;
    state.mouse.x = -9999;
    state.mouse.y = -9999;
  });

  canvas.addEventListener("pointerdown", () => {
    state.mouse.down = true;
  });

  window.addEventListener("pointerup", () => {
    state.mouse.down = false;
  });

  resize();
  initParticles();
  setMode("now");
  setActivity("idle");
  renderQueue();
  loadHealth();
  loadSessions();
  loadTasks();
  requestAnimationFrame(animate);
})();
