import React from "react";
import { AbsoluteFill, Easing, interpolate, useCurrentFrame } from "remotion";
import { C, COUNTS, ENDING, FLURRY, OPENING, SESSIONS, Session, TURN } from "./script";

const FONT = '"Menlo", "PingFang SC", monospace';
const clamp = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;
const ease = Easing.bezier(0.45, 0, 0.25, 1);

const X = 120; // 左边距
const WORK_TOP = 190; // 工作区第一行
const ROW = 52;
const ARCH_BOTTOM = 1000; // 档案最新一行
const ARCH_ROW = 41;

// 档案：每一句什么时候落下
type Entry = { tag: string; text: string; at: number; pit?: boolean };
const ENTRIES: Entry[] = [
  ...SESSIONS.map((s) => ({ tag: s.tag, text: s.keep, at: s.to - 4 })),
  ...FLURRY.titles.map((t, i) => ({ tag: "踩坑", text: t, at: FLURRY.from + i * FLURRY.every, pit: true })),
].sort((a, b) => a.at - b.at);

const typed = (text: string, frame: number, from: number, perChar = 2) =>
  frame < from ? "" : text.slice(0, Math.floor((frame - from) / perChar) + 1);

const Cursor: React.FC<{ frame: number; on?: boolean }> = ({ frame, on = true }) =>
  on && Math.floor(frame / 15) % 2 === 0 ? (
    <span style={{ display: "inline-block", width: 14, height: 28, background: C.amber, marginLeft: 6, verticalAlign: -4 }} />
  ) : null;

const sessionLines = (s: Session) => {
  const lines = s.lines.map((t) => ({ t, keep: false }));
  lines.splice(s.keepAt, 0, { t: s.keep, keep: true });
  return lines;
};

const SessionView: React.FC<{ s: Session; frame: number }> = ({ s, frame }) => {
  const lines = sessionLines(s);
  const gap = Math.max(6, Math.floor((s.to - s.from - 64) / lines.length));
  const collapse = s.to - 34;
  const meter = interpolate(frame, [s.from, collapse], [0, 1], clamp);
  const full = frame >= collapse;
  const headerOpacity = interpolate(frame, [s.from, s.from + 6, s.to - 8, s.to], [0, 1, 1, 0], clamp);
  return (
    <>
      <div style={{ position: "absolute", left: X, top: 92, right: X, display: "flex", fontFamily: FONT, fontSize: 22, color: C.dim, opacity: headerOpacity }}>
        <span>会话 · {s.agent}</span>
        <span style={{ marginLeft: "auto" }}>{s.date}</span>
        <span style={{ marginLeft: 28, width: 260, height: 6, marginTop: 10, background: C.faint, position: "relative" }}>
          <span style={{ position: "absolute", left: 0, top: 0, bottom: 0, width: `${meter * 100}%`, background: full ? C.amber : C.dim }} />
        </span>
        <span style={{ marginLeft: 16, width: 150, color: full ? C.amber : C.dim }}>{full ? "上下文已满" : `${Math.round(meter * 100)}%`}</span>
      </div>
      {lines.map((l, i) => {
        const at = s.from + 10 + i * gap;
        if (frame < at) return null;
        const shown = l.t.slice(0, Math.ceil(interpolate(frame, [at, at + 8], [1, l.t.length], clamp)));
        const y = WORK_TOP + i * ROW;
        if (!l.keep) {
          const o = interpolate(frame, [collapse, collapse + 14], [1, 0], clamp);
          const sy = interpolate(frame, [collapse, collapse + 14], [1, 0.2], { ...clamp, easing: ease });
          return (
            <div key={i} style={{ position: "absolute", left: X, top: y, fontFamily: FONT, fontSize: 30, color: C.text, opacity: o * 0.85, transform: `scaleY(${sy})`, transformOrigin: "left center", whiteSpace: "nowrap" }}>
              <span style={{ color: C.dim }}>› </span>
              {shown}
            </div>
          );
        }
        // 被留下的那一句：变色，落进档案
        const fall = interpolate(frame, [collapse + 10, s.to - 4], [0, 1], { ...clamp, easing: ease });
        const ty = interpolate(fall, [0, 1], [y, ARCH_BOTTOM]);
        const size = interpolate(fall, [0, 1], [30, 24]);
        return (
          <div key={i} style={{ position: "absolute", left: X, top: ty, fontFamily: FONT, fontSize: size, color: frame >= collapse ? C.amber : C.text, whiteSpace: "nowrap", opacity: frame >= s.to - 4 ? 0 : 1 }}>
            <span style={{ color: C.dim }}>{frame >= collapse ? `${s.tag} · ` : "› "}</span>
            {shown}
          </div>
        );
      })}
    </>
  );
};

const Archive: React.FC<{ frame: number }> = ({ frame }) => {
  const landed = ENTRIES.filter((e) => frame >= e.at);
  const expand = interpolate(frame, TURN.expand, [0, 1], { ...clamp, easing: ease });
  const endFade = interpolate(frame, [ENDING.from, ENDING.from + 15], [1, 0], clamp);
  // 顺滑上推：后面每落下一句，前面的就上移一行
  const slide = (e: Entry) => interpolate(frame, [e.at, e.at + 6], [0, 1], { ...clamp, easing: ease });
  const dividerY = interpolate(expand, [0, 1], [790, 160]);
  return (
    <div style={{ opacity: endFade }}>
      <div style={{ position: "absolute", left: X, right: X, top: dividerY, height: 1, background: C.faint }} />
      <div style={{ position: "absolute", left: X, top: dividerY - 34, fontFamily: FONT, fontSize: 20, color: C.dim, opacity: frame >= 150 ? 1 : 0 }}>
        档案 · {landed.length} 行
      </div>
      {frame >= TURN.expand[1] ? (
        <div style={{ position: "absolute", right: X, top: dividerY - 34, fontFamily: FONT, fontSize: 20, color: C.dim, opacity: interpolate(frame, [TURN.expand[1], TURN.expand[1] + 15], [0, 1], clamp) }}>
          {COUNTS}
        </div>
      ) : null}
      {landed.map((e, idx) => {
        const later = landed.slice(idx + 1).reduce((acc, n) => acc + slide(n), 0);
        const fresh = slide(e);
        const y = ARCH_BOTTOM - later * ARCH_ROW + (1 - fresh) * ARCH_ROW; // 新的一行从下方滑进来
        const normal = later < 0.5 ? 1 : Math.max(0, 1 - later * 0.2);
        const o = interpolate(expand, [0, 1], [normal, 1]) * fresh;
        if (y < dividerY + 10 || o <= 0.01) return null;
        // 转折段：读档案的高亮依次扫过
        let reading = false;
        if (frame >= TURN.agentsFrom && frame < TURN.to) {
          const t = ((frame - TURN.agentsFrom) % TURN.agentEvery) / TURN.agentEvery;
          const row = Math.floor(t * landed.length);
          reading = landed.length - 1 - row === idx;
        }
        return (
          <div key={idx} style={{ position: "absolute", left: X - 12, right: X, top: y, padding: "0 12px", fontFamily: FONT, fontSize: 24, color: reading ? C.bg : e.pit ? C.dim : C.text, background: reading ? C.amber : "transparent", opacity: o, whiteSpace: "nowrap" }}>
            <span style={{ color: reading ? C.bg : C.dim }}>{e.tag} · </span>
            {e.text}
          </div>
        );
      })}
    </div>
  );
};

const Opening: React.FC<{ frame: number }> = ({ frame }) => {
  if (frame >= SESSIONS[0].from) return null;
  const o = interpolate(frame, [SESSIONS[0].from - 14, SESSIONS[0].from], [1, 0], clamp);
  const l1 = typed(OPENING.line1, frame, 40, 3);
  const l2 = typed(OPENING.line2, frame, 140, 3);
  const typingDone = frame > 140 + OPENING.line2.length * 3;
  return (
    <div style={{ opacity: o, fontFamily: FONT, fontSize: 30, color: C.text }}>
      <div style={{ position: "absolute", left: X, top: 92, fontSize: 22, color: C.dim, opacity: frame >= 20 ? 1 : 0 }}>2026-08-22</div>
      <div style={{ position: "absolute", left: X, top: WORK_TOP }}>
        {l1}
        {frame < 140 ? <Cursor frame={frame} /> : null}
      </div>
      <div style={{ position: "absolute", left: X, top: WORK_TOP + 54 }}>
        {l2}
        {frame >= 140 ? <Cursor frame={frame} on={typingDone ? true : true} /> : null}
      </div>
    </div>
  );
};

const Turn: React.FC<{ frame: number }> = ({ frame }) => {
  if (frame < TURN.from || frame >= TURN.to) return null;
  const agentIdx = frame < TURN.agentsFrom ? 0 : Math.min(TURN.agents.length - 1, Math.floor((frame - TURN.agentsFrom) / TURN.agentEvery));
  const agent = TURN.agents[agentIdx];
  const wakeO = interpolate(frame, [TURN.expand[0] - 10, TURN.expand[0] + 10], [1, 0], clamp);
  const headO = interpolate(frame, [TURN.from, TURN.from + 8, TURN.to - 8, TURN.to], [0, 1, 1, 0], clamp);
  return (
    <>
      <div style={{ position: "absolute", left: X, top: 92, fontFamily: FONT, fontSize: 22, color: C.dim, opacity: headO }}>
        会话 · <span style={{ color: frame >= TURN.agentsFrom ? C.amber : C.dim }}>{agent}</span>
        {frame >= TURN.expand[0] ? " · 读取档案" : ""}
      </div>
      <div style={{ position: "absolute", left: X, top: WORK_TOP, fontFamily: FONT, fontSize: 30, color: C.text, opacity: wakeO }}>
        {typed(OPENING.line1, frame, TURN.wake, 2)}
        <Cursor frame={frame} />
      </div>
    </>
  );
};

const Ending: React.FC<{ frame: number }> = ({ frame }) => {
  if (frame < ENDING.from) return null;
  const out = interpolate(frame, [2100, 2128], [1, 0], clamp);
  const l1 = typed(ENDING.l1, frame, ENDING.l1At, 3);
  const l2 = typed(ENDING.l2, frame, ENDING.l2At, 4);
  return (
    <div style={{ opacity: out, fontFamily: FONT }}>
      <div style={{ position: "absolute", left: X, top: 430, fontSize: 40, color: C.text }}>{l1}</div>
      <div style={{ position: "absolute", left: X, top: 500, fontSize: 40, color: C.amber }}>
        {l2}
        {frame >= ENDING.l2At ? <Cursor frame={frame} /> : null}
      </div>
      <div style={{ position: "absolute", left: X, bottom: 80, fontSize: 20, color: C.dim, opacity: interpolate(frame, [ENDING.titleAt, ENDING.titleAt + 20], [0, 1], clamp) }}>
        小八 AgentOS · 2026.08.22 — 2026.10.14
      </div>
    </div>
  );
};

export const Memory: React.FC = () => {
  const frame = useCurrentFrame();
  const s = SESSIONS.find((x) => frame >= x.from && frame < x.to);
  return (
    <AbsoluteFill style={{ backgroundColor: C.bg }}>
      <Opening frame={frame} />
      {s ? <SessionView s={s} frame={frame} /> : null}
      <Turn frame={frame} />
      <Archive frame={frame} />
      <Ending frame={frame} />
    </AbsoluteFill>
  );
};
