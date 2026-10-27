import React, { useEffect, useState } from "react";
import {
  AbsoluteFill,
  Easing,
  continueRender,
  delayRender,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import {
  CAMERA,
  CAPTIONS,
  COLORS,
  LABELS,
  LEAVES,
  PULLBACK,
  STROKES,
  Stroke,
  Y,
} from "./timeline";

const FONT = "XiaobaRound";
const ease = Easing.bezier(0.45, 0, 0.25, 1); // 起步慢、收尾稳

const clampOpts = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;

const fade = (frame: number, from: number, to?: number, len = 10) => {
  const fin = interpolate(frame, [from, from + len], [0, 1], clampOpts);
  const fout = to === undefined ? 1 : interpolate(frame, [to - len, to], [1, 0], clampOpts);
  return Math.min(fin, fout);
};

const useFont = () => {
  const [handle] = useState(() => delayRender("load font", { timeoutInMilliseconds: 120000 }));
  useEffect(() => {
    const face = new FontFace(FONT, `url(${staticFile("fonts/FangYuan.ttf")})`);
    face
      .load()
      .then((f) => {
        document.fonts.add(f);
        continueRender(handle);
      })
      .catch((err) => {
        console.error(err);
        continueRender(handle);
      });
  }, [handle]);
};

// 一段会被“画出来”的线
const DrawnPath: React.FC<{ s: Stroke; frame: number }> = ({ s, frame }) => {
  let p = interpolate(frame, [s.from, s.to], [0, 1], { ...clampOpts, easing: ease });
  if (s.erase) {
    p *= interpolate(frame, s.erase, [1, 0], { ...clampOpts, easing: ease });
  }
  if (p <= 0) return null;
  const fillOpacity =
    s.fill && s.fillFrom !== undefined
      ? interpolate(frame, [s.fillFrom, s.fillFrom + 8], [0, 1], clampOpts) *
        (s.erase ? interpolate(frame, s.erase, [1, 0], clampOpts) : 1)
      : 0;
  return (
    <path
      d={s.d}
      pathLength={1}
      fill={s.fill ?? "none"}
      fillOpacity={fillOpacity}
      stroke={s.color ?? COLORS.ink}
      strokeWidth={s.width ?? 5}
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeDasharray="1 1"
      strokeDashoffset={1 - p}
      vectorEffect="non-scaling-stroke"
    />
  );
};

const Grain: React.FC<{ frame: number }> = ({ frame }) => {
  const seed = Math.floor(frame / 2) % 12;
  return (
    <AbsoluteFill style={{ pointerEvents: "none", mixBlendMode: "multiply", opacity: 0.07 }}>
      <svg width="1920" height="1080">
        <filter id="grain">
          <feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="2" seed={seed} stitchTiles="stitch" />
          <feColorMatrix type="saturate" values="0" />
        </filter>
        <rect width="1920" height="1080" filter="url(#grain)" />
      </svg>
    </AbsoluteFill>
  );
};

const Caption: React.FC<{ frame: number }> = ({ frame }) => {
  const c = CAPTIONS.find((x) => frame >= x.from && frame < x.to);
  if (!c) return null;
  const o = fade(frame, c.from, c.to, 12);
  const dy = interpolate(frame, [c.from, c.from + 14], [10, 0], { ...clampOpts, easing: ease });
  return (
    <div
      style={{
        position: "absolute",
        left: 110,
        top: 96,
        opacity: o,
        transform: `translateY(${dy}px)`,
        fontFamily: FONT,
        color: COLORS.ink,
      }}
    >
      <div style={{ fontSize: 26, color: COLORS.mute, letterSpacing: 2 }}>{c.date}</div>
      <div style={{ fontSize: 56, marginTop: 10, lineHeight: 1.2 }}>{c.title}</div>
      {c.sub ? <div style={{ fontSize: 28, marginTop: 14, color: COLORS.mute, maxWidth: 1300 }}>{c.sub}</div> : null}
    </div>
  );
};

const Opening: React.FC<{ frame: number }> = ({ frame }) => {
  if (frame > 210) return null;
  const a = fade(frame, 18, 100, 14);
  const b = fade(frame, 104, 206, 14);
  return (
    <AbsoluteFill style={{ fontFamily: FONT, color: COLORS.ink, alignItems: "center" }}>
      <div style={{ position: "absolute", top: 330, fontSize: 52, opacity: a }}>这是小八的 53 天。</div>
      <div style={{ position: "absolute", top: 250, textAlign: "center", opacity: b }}>
        <div style={{ fontSize: 26, color: COLORS.mute, letterSpacing: 2 }}>2026.08.22 · 第一份笔记里写着</div>
        <div style={{ fontSize: 44, marginTop: 22, lineHeight: 1.5 }}>
          项目结束后的长期资产是规则、索引和 QC 经验，
          <br />
          不是每个版本的媒体文件。
        </div>
      </div>
    </AbsoluteFill>
  );
};

const Ending: React.FC<{ frame: number }> = ({ frame }) => {
  if (frame < PULLBACK.to) return null;
  const a = fade(frame, PULLBACK.to, undefined, 16);
  const b = fade(frame, PULLBACK.to + 18, undefined, 16);
  const c = fade(frame, PULLBACK.to + 40, undefined, 16);
  return (
    <AbsoluteFill style={{ fontFamily: FONT, color: COLORS.ink, alignItems: "center" }}>
      <div style={{ position: "absolute", top: 150, fontSize: 30, color: COLORS.accent, opacity: a, letterSpacing: 4 }}>
        小八 AgentOS
      </div>
      <div style={{ position: "absolute", top: 205, fontSize: 30, color: COLORS.mute, opacity: a }}>53 天 · 26 个版本</div>
      <div style={{ position: "absolute", top: 270, fontSize: 66, opacity: b }}>经理可以换，企业不能倒。</div>
      <div style={{ position: "absolute", right: 110, bottom: 70, fontSize: 24, color: COLORS.mute, opacity: c }}>
        整理：小克 · 2026.10.14
      </div>
    </AbsoluteFill>
  );
};

export const Growth: React.FC = () => {
  useFont();
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  // 镜头：跟着笔尖走，最后拉远看整条线
  const camX = interpolate(
    frame,
    CAMERA.map((k) => k[0]),
    CAMERA.map((k) => k[1]),
    { ...clampOpts, easing: ease },
  );
  const pb = interpolate(frame, [PULLBACK.from, PULLBACK.to], [0, 1], {
    ...clampOpts,
    easing: Easing.bezier(0.6, 0, 0.2, 1),
  });
  const scale = interpolate(pb, [0, 1], [1, PULLBACK.scale]);
  const cx = interpolate(pb, [0, 1], [camX, PULLBACK.centerX]);
  const cy = interpolate(pb, [0, 1], [Y, PULLBACK.centerY]);
  const labelsOut = interpolate(frame, [PULLBACK.from, PULLBACK.from + 24], [1, 0], clampOpts);

  const dot = spring({ frame: frame - 40, fps, config: { damping: 12, mass: 0.6 } });
  const seal = spring({ frame: frame - 450, fps, config: { damping: 14, stiffness: 180 } });
  const pupil = spring({ frame: frame - 1116, fps, config: { damping: 12 } });
  const blink = interpolate(frame, [1150, 1155, 1160], [1, 0.12, 1], clampOpts);
  const drift = interpolate(frame, [1690, 1730], [0, 22], { ...clampOpts, easing: ease });
  const travel = frame > 2010 ? ((frame - 2010) / 96) % 1 : 0;

  return (
    <AbsoluteFill style={{ backgroundColor: COLORS.paper }}>
      <svg width="1920" height="1080" style={{ position: "absolute" }}>
        <defs>
          <filter id="hand" x="-5%" y="-20%" width="110%" height="140%">
            <feTurbulence type="fractalNoise" baseFrequency="0.006" numOctaves="1" seed="7" />
            <feDisplacementMap in="SourceGraphic" scale="2.2" />
          </filter>
          <radialGradient id="vignette" cx="50%" cy="50%" r="75%">
            <stop offset="60%" stopColor={COLORS.paper} stopOpacity="0" />
            <stop offset="100%" stopColor="#C9D9F0" stopOpacity="0.45" />
          </radialGradient>
        </defs>
        <g transform={`translate(960 540) scale(${scale}) translate(${-cx} ${-cy})`}>
          <g filter="url(#hand)">
            {/* 起点 */}
            {frame >= 40 ? <circle cx={600} cy={Y} r={9 * dot} fill={COLORS.ink} /> : null}
            {STROKES.filter((s) => !s.id.startsWith("branch")).map((s) => (
              <DrawnPath key={s.id} s={s} frame={frame} />
            ))}
            {/* 第二大脑：枝和叶，脱钩后整体落到自己的地面上 */}
            <g transform={`translate(0 ${drift})`}>
              {STROKES.filter((s) => s.id.startsWith("branch")).map((s) => (
                <DrawnPath key={s.id} s={s} frame={frame} />
              ))}
              {LEAVES.map((l, i) => {
                const k = spring({ frame: frame - l.at, fps, config: { damping: 11, mass: 0.5 } });
                return k > 0.01 ? (
                  <circle
                    key={i}
                    cx={l.x}
                    cy={l.y}
                    r={l.r * k}
                    fill={COLORS.soft}
                    stroke={COLORS.ink}
                    strokeWidth={3}
                    vectorEffect="non-scaling-stroke"
                  />
                ) : null;
              })}
            </g>
            {/* 监看中心的瞳孔 */}
            {frame >= 1116 ? (
              <g transform={`translate(5450 ${Y}) scale(1 ${blink})`}>
                <circle r={46 * pupil} fill={COLORS.soft} stroke={COLORS.ink} strokeWidth={5} vectorEffect="non-scaling-stroke" />
                <circle r={18 * pupil} fill={COLORS.ink} />
              </g>
            ) : null}
          </g>

          {/* 结尾「8」上流动的一小段亮色 */}
          {frame > 2010 ? (
            <path
              d={STROKES.find((s) => s.id === "eight")!.d}
              pathLength={1}
              fill="none"
              stroke={COLORS.soft}
              strokeWidth={interpolate(pb, [0, 1], [14, 9])}
              strokeLinecap="round"
              strokeDasharray="0.07 0.93"
              strokeDashoffset={-travel}
              vectorEffect="non-scaling-stroke"
              opacity={0.9}
            />
          ) : null}

          {/* 用户裁决的印章：全片唯一的深蓝 */}
          {frame >= 450 ? (
            <g
              transform={`translate(2470 ${Y}) rotate(-6) scale(${interpolate(seal, [0, 1], [1.6, 1])})`}
              opacity={interpolate(frame, [450, 455], [0, 1], clampOpts)}
            >
              <rect x={-48} y={-48} width={96} height={96} rx={20} fill={COLORS.accent} />
              <text
                textAnchor="middle"
                dominantBaseline="central"
                fontFamily={FONT}
                fontSize={46}
                fill={COLORS.paper}
              >
                裁
              </text>
            </g>
          ) : null}

          {/* 世界里的标注字 */}
          {LABELS.map((l, i) => {
            const o = fade(frame, l.from, l.to, 10) * labelsOut;
            return o > 0 ? (
              <text
                key={i}
                x={l.x}
                y={l.y + (l.text.startsWith("第二大脑") ? drift : 0)}
                textAnchor="middle"
                fontFamily={FONT}
                fontSize={l.size ?? 30}
                fill={l.color ?? COLORS.mute}
                opacity={o}
              >
                {l.text}
              </text>
            ) : null;
          })}
        </g>
        <rect width="1920" height="1080" fill="url(#vignette)" />
      </svg>
      <Opening frame={frame} />
      <Caption frame={frame} />
      <Ending frame={frame} />
      <Grain frame={frame} />
    </AbsoluteFill>
  );
};
