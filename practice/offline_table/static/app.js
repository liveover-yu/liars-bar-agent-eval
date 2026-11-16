(function () {
  "use strict";

  const STORAGE_KEY = "liars-bar-room-session";
  const POLL_INTERVAL_MS = 2500;
  const byId = (id) => document.getElementById(id);

  const ui = {
    entry: byId("entry-screen"),
    game: byId("game-screen"),
    entryError: byId("entry-error"),
    gameError: byId("error-message"),
    connection: byId("connection-status"),
    create: byId("create-room"),
    joinForm: byId("join-form"),
    join: byId("join-room"),
    codeInput: byId("room-code-input"),
    roomCode: byId("room-code"),
    mySeat: byId("my-seat"),
    copyCode: byId("copy-code"),
    copyLink: byId("copy-link"),
    refresh: byId("refresh-state"),
    phase: byId("phase-label"),
    round: byId("round-label"),
    target: byId("target-card"),
    turn: byId("turn-indicator"),
    notice: byId("game-notice"),
    resultPanel: byId("result-panel"),
    resultSummary: byId("result-summary"),
    resultCards: byId("result-cards"),
    handCount: byId("hand-count"),
    handList: byId("hand-list"),
    toggleHand: byId("toggle-hand"),
    actionTitle: byId("action-title"),
    actionHelp: byId("action-help"),
    start: byId("start-game"),
    play: byId("play-cards"),
    challenge: byId("challenge-play"),
    pass: byId("pass-challenge"),
    advance: byId("advance-round"),
    restart: byId("restart-game")
  };

  let session = readSession();
  let state = null;
  let busy = false;
  let refreshing = false;
  let mutationSerial = 0;
  let handVisible = false;
  let handSignature = "";
  let handRenderKey = "";
  let selected = new Set();

  function readSession() {
    try {
      const value = JSON.parse(sessionStorage.getItem(STORAGE_KEY));
      if (value && value.room_code && value.token) return value;
    } catch (_) {
      // A malformed or unavailable session is treated as a new visit.
    }
    return null;
  }

  function saveSession(value) {
    session = {
      room_code: String(value.room_code),
      seat: value.seat,
      token: String(value.token)
    };
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(session));
  }

  function roomPath(suffix) {
    return "/api/rooms/" + encodeURIComponent(session.room_code) + suffix;
  }

  async function api(method, path, body, needsToken) {
    const headers = { Accept: "application/json" };
    if (needsToken) {
      if (!session || !session.token) throw new Error("当前席位凭证已丢失，请重新打开你的房间链接。");
      headers.Authorization = "Bearer " + session.token;
    }
    if (body !== undefined) headers["Content-Type"] = "application/json";

    let response;
    try {
      response = await fetch(path, {
        method: method,
        headers: headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        cache: "no-store"
      });
    } catch (_) {
      throw new Error("无法连接本地牌桌服务，请确认服务仍在运行。");
    }

    const contentType = response.headers.get("content-type") || "";
    let data;
    if (contentType.includes("application/json")) {
      try {
        data = await response.json();
      } catch (_) {
        throw new Error("服务返回的数据无法读取，请刷新牌局后重试。");
      }
    } else {
      data = await response.text();
    }

    if (!response.ok) {
      const detail = data && typeof data === "object"
        ? (data.error || data.message || data.detail)
        : null;
      const error = new Error(detail ? String(detail) : "请求失败（" + response.status + "），请刷新牌局后重试。");
      error.status = response.status;
      throw error;
    }
    if (!data || typeof data !== "object") {
      throw new Error("服务没有返回有效的牌局数据，请刷新牌局后重试。");
    }
    return data;
  }

  function showEntry() {
    ui.entry.hidden = false;
    ui.game.hidden = true;
  }

  function showGame() {
    ui.entry.hidden = true;
    ui.game.hidden = false;
    if (session) {
      ui.roomCode.textContent = session.room_code;
      ui.mySeat.textContent = "你的席位：" + String(session.seat);
    }
  }

  function setConnection(message) {
    ui.connection.textContent = message;
  }

  function showError(message) {
    const target = ui.game.hidden ? ui.entryError : ui.gameError;
    target.textContent = message;
    target.hidden = false;
    setConnection("连接需要检查");
  }

  function clearError() {
    ui.entryError.hidden = true;
    ui.gameError.hidden = true;
    ui.entryError.textContent = "";
    ui.gameError.textContent = "";
  }

  function hideHand() {
    handVisible = false;
    selected.clear();
    handRenderKey = "";
  }

  function forgetExpiredSession() {
    sessionStorage.removeItem(STORAGE_KEY);
    session = null;
    state = null;
    handSignature = "";
    hideHand();
    showEntry();
    clearError();
    showError("原房间已失效。若本地服务刚重启，请重新创建房间；也可以输入仍有效的房间码加入。");
    setConnection("请重新入座");
    renderActions();
  }

  function receiveState(next) {
    if (!next || !Array.isArray(next.seats) || !Array.isArray(next.hand)) {
      throw new Error("牌局状态不完整，请刷新牌局后重试。");
    }
    const nextHandSignature = JSON.stringify({
      round: next.round,
      phase: next.phase === "over" ? "over" : "active",
      hand: next.hand
    });
    if (nextHandSignature !== handSignature) {
      hideHand();
      handSignature = nextHandSignature;
    }
    state = next;
    showGame();
    render();
    clearError();
    setConnection("牌局已同步");
  }

  function sameSeat(a, b) {
    if (a === null || a === undefined || b === null || b === undefined) return false;
    return String(a) === String(b);
  }

  function renderSeats() {
    const seatElements = document.querySelectorAll(".seat[data-seat-index]");
    seatElements.forEach((element) => {
      const index = Number(element.dataset.seatIndex);
      const seat = state.seats[index];
      const name = document.createElement("span");
      const details = document.createElement("span");
      name.className = "seat-name";
      details.className = "seat-details";
      element.classList.remove("is-current", "is-me", "is-out");

      if (!seat || !seat.joined) {
        name.textContent = "空位";
        details.textContent = "等待加入";
      } else {
        const isMe = sameSeat(seat.id, state.seat);
        name.textContent = String(seat.id) + (isMe ? " · 你" : "");
        details.textContent = seat.alive
          ? "手牌 " + Number(seat.card_count || 0) + " 张"
          : "已淘汰";
        element.classList.toggle("is-me", isMe);
        element.classList.toggle("is-out", !seat.alive);
        element.classList.toggle("is-current",
          state.phase === "play" && sameSeat(seat.id, state.current_player) ||
          state.phase === "challenge" && sameSeat(seat.id, state.challenger));
      }
      element.replaceChildren(name, details);
    });
  }

  function renderResult() {
    const result = state.result;
    ui.resultPanel.hidden = !result;
    ui.resultCards.replaceChildren();
    if (!result) return;

    const truth = result.valid ? "出牌为真" : "出牌为假";
    const shot = result.shot === "dead" ? "中枪" :
      result.shot === "blank" ? "空枪" : "已执行处罚";
    ui.resultSummary.textContent = truth + "，" + String(result.penalty_player) + " 受罚：" + shot + "。";
    if (Array.isArray(result.cards)) {
      result.cards.forEach((card) => {
        const element = document.createElement("span");
        element.className = "result-card";
        element.textContent = String(card);
        ui.resultCards.appendChild(element);
      });
    }
  }

  function renderHand() {
    const canPlay = !!(state.can && state.can.play);
    const key = JSON.stringify({
      cards: state.hand,
      visible: handVisible,
      selected: Array.from(selected).sort(),
      canPlay: canPlay,
      busy: busy
    });
    ui.handCount.textContent = state.started
      ? "你有 " + state.hand.length + " 张牌。"
      : "开局后发牌。";
    ui.toggleHand.disabled = busy || state.hand.length === 0;
    ui.toggleHand.textContent = handVisible ? "遮住我的手牌" : "查看我的手牌";
    ui.toggleHand.setAttribute("aria-expanded", String(handVisible));
    if (key === handRenderKey) return;
    handRenderKey = key;
    ui.handList.replaceChildren();

    if (state.hand.length === 0) {
      const empty = document.createElement("p");
      empty.className = "hand-empty";
      empty.textContent = state.started ? "你目前没有手牌。" : "开局后，你的牌会出现在这里。";
      ui.handList.appendChild(empty);
      return;
    }

    state.hand.forEach((card, index) => {
      const element = document.createElement("button");
      element.type = "button";
      element.className = "hand-card";
      if (!handVisible) {
        element.classList.add("hand-card-back");
        element.textContent = "♠";
        element.setAttribute("aria-label", "盖住的手牌 " + (index + 1));
        element.disabled = true;
      } else {
        const cardText = String(card);
        element.textContent = cardText;
        if (cardText.toLowerCase() === "joker") element.classList.add("hand-card-joker");
        element.classList.toggle("is-selected", selected.has(index));
        element.setAttribute("aria-pressed", String(selected.has(index)));
        element.setAttribute("aria-label", cardText + "，" + (selected.has(index) ? "已选择" : "未选择"));
        element.disabled = busy || !canPlay;
        element.addEventListener("click", () => toggleCard(index));
      }
      ui.handList.appendChild(element);
    });
  }

  function toggleCard(index) {
    if (!handVisible || !state || !state.can || !state.can.play || busy) return;
    if (selected.has(index)) {
      selected.delete(index);
    } else if (selected.size < 3) {
      selected.add(index);
    } else {
      showError("每次最多选择 3 张牌。");
      return;
    }
    renderHand();
    renderActions();
  }

  function describeAction() {
    const phase = state.phase;
    const can = state.can || {};
    if (phase === "lobby") {
      const joined = state.seats.filter((seat) => seat.joined).length;
      return {
        title: "等待开局",
        help: can.start
          ? "已有 " + joined + " 位玩家入座。准备好后开始牌局。"
          : "已有 " + joined + " 位玩家入座。等待房主开局。"
      };
    }
    if (phase === "play") {
      return can.play
        ? { title: "轮到你出牌", help: "查看手牌，选择 1 到 3 张。目标牌是 " + String(state.target_card) + "；鬼牌也算真牌。" }
        : { title: "等待出牌", help: String(state.current_player) + " 正在选择要打出的牌。" };
    }
    if (phase === "challenge") {
      const play = state.last_play;
      const summary = play ? String(play.player) + " 打出了 " + String(play.count) + " 张牌。" : "上一手已打出。";
      if (can.challenge && state.forced_challenge) {
        return { title: "必须质疑", help: summary + " 你是唯一还有手牌的玩家，必须翻开这一手。" };
      }
      return can.challenge
        ? { title: "轮到你决定", help: summary + " 你可以质疑，也可以让牌局继续。" }
        : { title: "等待质疑", help: summary + " 等待 " + String(state.challenger) + " 作出决定。" };
    }
    if (phase === "result") {
      return {
        title: "这一局已结束",
        help: can.advance ? "查看翻牌和处罚结果，然后开始下一局。" : "等待房主开始下一局。"
      };
    }
    if (phase === "over") {
      return {
        title: String(state.winner) + " 获胜",
        help: can.restart ? "牌局结束。可以在这个房间重新开局。" : "牌局结束。等待房主重新开局。"
      };
    }
    return { title: "正在同步牌局", help: "请稍候或刷新牌局。" };
  }

  function renderActions() {
    ui.create.disabled = busy;
    ui.join.disabled = busy;
    ui.refresh.disabled = busy || !session;
    if (!state) return;

    const can = state.can || {};
    const description = describeAction();
    ui.actionTitle.textContent = description.title;
    ui.actionHelp.textContent = description.help;

    const controls = [
      [ui.start, !!can.start],
      [ui.play, !!can.play],
      [ui.challenge, !!can.challenge],
      [ui.pass, !!can.challenge && !state.forced_challenge],
      [ui.advance, !!can.advance],
      [ui.restart, !!can.restart]
    ];
    controls.forEach(([button, visible]) => {
      button.hidden = !visible;
      button.disabled = busy || (button === ui.play && (selected.size < 1 || selected.size > 3));
    });
  }

  function render() {
    if (!state) return;
    ui.roomCode.textContent = String(state.room_code || session.room_code);
    ui.mySeat.textContent = "你的席位：" + String(state.seat);
    ui.round.textContent = state.started && state.round
      ? "第 " + String(state.round) + " 局"
      : "等待入座";
    ui.target.textContent = state.started && state.target_card
      ? String(state.target_card)
      : "—";

    const phaseText = {
      lobby: "等待玩家入座",
      play: "出牌中",
      challenge: "等待质疑",
      result: "翻牌与处罚",
      over: "牌局结束"
    };
    ui.phase.textContent = phaseText[state.phase] || "牌局进行中";
    if (state.phase === "play") {
      ui.turn.textContent = String(state.current_player) + " 正在出牌";
    } else if (state.phase === "challenge") {
      ui.turn.textContent = String(state.challenger) + " 决定是否质疑";
    } else if (state.phase === "over") {
      ui.turn.textContent = String(state.winner) + " 获胜";
    } else {
      ui.turn.textContent = state.started ? "等待下一步" : "等候开局";
    }

    ui.notice.textContent = state.notice || (
      state.phase === "lobby" ? "邀请其他玩家加入房间。" : "等待牌局继续。"
    );
    renderSeats();
    renderResult();
    renderHand();
    renderActions();
  }

  async function refreshState() {
    if (!session || busy || refreshing) return;
    refreshing = true;
    const startSerial = mutationSerial;
    try {
      const next = await api("GET", roomPath("/state"), undefined, true);
      if (startSerial === mutationSerial) receiveState(next);
    } catch (error) {
      if (startSerial === mutationSerial) {
        if ([401, 403, 404].includes(error.status)) {
          forgetExpiredSession();
        } else {
          showError("同步牌局失败：" + error.message);
        }
      }
    } finally {
      refreshing = false;
    }
  }

  async function enterRoom(method, path, body) {
    if (busy) return;
    busy = true;
    clearError();
    setConnection("正在入座");
    renderActions();
    try {
      const credentials = await api(method, path, body, false);
      if (!credentials.room_code || !credentials.token || credentials.seat === undefined) {
        throw new Error("服务没有返回完整的房间凭证。");
      }
      saveSession(credentials);
      showGame();
      const next = await api("GET", roomPath("/state"), undefined, true);
      receiveState(next);
      const url = new URL(window.location.href);
      url.searchParams.set("room", session.room_code);
      window.history.replaceState(null, "", url);
    } catch (error) {
      showError("入座失败：" + error.message);
    } finally {
      busy = false;
      renderActions();
    }
  }

  async function mutate(path, body) {
    if (!session || busy) return;
    busy = true;
    mutationSerial += 1;
    clearError();
    setConnection("正在提交操作");
    renderActions();
    try {
      const next = await api("POST", roomPath(path), body, true);
      hideHand();
      receiveState(next);
    } catch (error) {
      hideHand();
      renderHand();
      showError("操作失败：" + error.message);
    } finally {
      busy = false;
      renderHand();
      renderActions();
    }
  }

  async function copyText(value, successMessage) {
    try {
      let copied = false;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        try {
          await navigator.clipboard.writeText(value);
          copied = true;
        } catch (_) {
          // Some local-network browsers require the older copy path over HTTP.
        }
      }
      if (!copied) {
        const input = document.createElement("textarea");
        input.value = value;
        input.style.position = "fixed";
        input.style.left = "-9999px";
        document.body.appendChild(input);
        input.select();
        copied = document.execCommand("copy");
        input.remove();
        if (!copied) throw new Error("copy unavailable");
      }
      clearError();
      setConnection(successMessage);
    } catch (_) {
      showError("复制失败。请手动选择房间码或浏览器地址栏中的邀请链接。");
    }
  }

  ui.create.addEventListener("click", () => enterRoom("POST", "/api/rooms"));
  ui.joinForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const code = ui.codeInput.value.trim().toUpperCase();
    if (!code) {
      showError("请输入房间码后再加入。");
      ui.codeInput.focus();
      return;
    }
    enterRoom("POST", "/api/rooms/" + encodeURIComponent(code) + "/join");
  });
  ui.refresh.addEventListener("click", refreshState);
  ui.copyCode.addEventListener("click", () => {
    if (session) copyText(session.room_code, "房间码已复制");
  });
  ui.copyLink.addEventListener("click", () => {
    if (!session) return;
    const url = new URL(window.location.href);
    url.searchParams.set("room", session.room_code);
    copyText(url.toString(), "邀请链接已复制");
  });
  ui.toggleHand.addEventListener("click", () => {
    if (!state || state.hand.length === 0 || busy) return;
    handVisible = !handVisible;
    if (!handVisible) selected.clear();
    renderHand();
    renderActions();
  });
  ui.start.addEventListener("click", () => mutate("/start"));
  ui.play.addEventListener("click", () => {
    if (!state || selected.size < 1 || selected.size > 3) return;
    const cards = Array.from(selected).sort((a, b) => a - b).map((index) => state.hand[index]);
    mutate("/play", { cards: cards });
  });
  ui.challenge.addEventListener("click", () => mutate("/challenge", { challenge: true }));
  ui.pass.addEventListener("click", () => mutate("/challenge", { challenge: false }));
  ui.advance.addEventListener("click", () => mutate("/advance"));
  ui.restart.addEventListener("click", () => mutate("/restart"));

  const invitedRoom = new URLSearchParams(window.location.search).get("room");
  if (invitedRoom) ui.codeInput.value = invitedRoom.trim().toUpperCase();
  if (session) {
    showGame();
    setConnection("正在同步牌局");
    refreshState();
  } else {
    showEntry();
  }
  window.setInterval(refreshState, POLL_INTERVAL_MS);
})();
