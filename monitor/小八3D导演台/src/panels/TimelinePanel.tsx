import {
  Box,
  Camera as CameraIcon,
  Clapperboard,
  Diamond,
  Download,
  Pause,
  Play,
  SkipBack,
} from "lucide-react";
import { useMemo } from "react";
import { EASES, type Ease } from "../core";
import { spatialReport } from "../spatial";
import { active, useStore } from "../store";
import { Field } from "../ui";
export function TimelinePanel({
  edgeCapture,
}: {
  edgeCapture: () => Promise<void>;
}) {
  const s = useStore(),
    shot = active(s.project);
  // Memoize whole-timeline analysis to revisions and shot id, completely eliminating seek recalculations
  const spatial = useMemo(() => spatialReport(shot), [s.revision, shot.id]);
  return (
    <section className="timeline">
      <div className="timeline-toolbar">
        <div>
          <Clapperboard size={16} />
          <input
            aria-label="镜头名称"
            value={shot.name}
            onChange={(e) => s.edit((sh) => (sh.name = e.target.value))}
          />
          <span className="badge">自动保存在当前会话</span>
        </div>
        <div className="transport">
          <button
            title="回到首帧"
            onClick={() => {
              s.set({ playing: false });
              s.seek(shot.in);
            }}
          >
            <SkipBack />
          </button>
          <button className="play" title="播放 / 暂停" onClick={s.togglePlay}>
            {s.playing ? <Pause /> : <Play />}
          </button>
          <button
            title="上一帧"
            onClick={() => {
              s.set({ playing: false });
              s.seek(s.time - 1 / 24);
            }}
          >
            −1f
          </button>
          <button
            title="下一帧"
            onClick={() => {
              s.set({ playing: false });
              s.seek(s.time + 1 / 24);
            }}
          >
            +1f
          </button>
          <b>
            {String(Math.floor(s.time)).padStart(2, "0")}:
            {String(Math.round((s.time % 1) * 24)).padStart(2, "0")}
          </b>
          <span>/ {shot.duration}s</span>
          <Field
            label="定位秒"
            value={s.time}
            min={0}
            max={shot.duration}
            step={1 / 24}
            onChange={(n) => {
              s.set({ playing: false });
              s.seek(n);
            }}
          />
          <button
            title="记录选中对象或相机关键帧"
            onClick={() => s.key(s.selected)}
          >
            <Diamond />
            打关键帧
          </button>
          <label className="check">
            <input
              type="checkbox"
              checked={s.autoKey}
              onChange={(e) => s.set({ autoKey: e.target.checked })}
            />
            自动打帧
          </label>
          {(() => {
            // 运动曲线作用于“当前时刻的关键帧 → 下一个关键帧”这一段；选中对象有键时只改它，否则改这一时刻的全部键。
            const here = shot.keys.filter((k) => Math.abs(k.time - s.time) < 1 / 48);
            const mine = here.filter((k) => k.target === s.selected);
            const scope = mine.length ? mine : here;
            const eases = [...new Set(scope.map((k) => k.ease ?? "linear"))];
            const apply = (ease: Ease, all: boolean) =>
              s.edit((sh) => {
                const ids = new Set(scope.map((k) => k.id));
                for (const k of sh.keys)
                  if (all || ids.has(k.id)) {
                    if (ease === "linear") delete k.ease;
                    else k.ease = ease;
                  }
              });
            return (
              <label
                className="check ease-picker"
                title={
                  scope.length
                    ? "从当前关键帧出发、到下一个关键帧之间的加减速方式"
                    : "先把时间定位到一个关键帧上，再设置它之后这一段的运动曲线"
                }
              >
                运动曲线
                <select
                  disabled={!scope.length}
                  value={eases.length === 1 ? eases[0] : ""}
                  onChange={(e) => apply(e.target.value as Ease, false)}
                >
                  {eases.length !== 1 && <option value="">{scope.length ? "（混合）" : "—"}</option>}
                  {EASES.map((e) => (
                    <option key={e.id} value={e.id}>
                      {e.label}
                    </option>
                  ))}
                </select>
                <button
                  disabled={!scope.length || eases.length !== 1}
                  title="把这个运动曲线用到本镜头的全部关键帧"
                  onClick={() => apply(eases[0], true)}
                >
                  全部应用
                </button>
              </label>
            );
          })()}
        </div>
        <div>
          <Field
            label="时长"
            value={shot.duration}
            min={1}
            max={600}
            step={1}
            onChange={(n) => {
              s.set({ playing: false });
              s.edit((sh) => {
                const followsEnd = sh.out === sh.duration;
                sh.duration = n;
                sh.out = followsEnd ? n : Math.min(sh.out, n);
                sh.in = Math.min(sh.in, n);
                sh.keys = sh.keys.filter((k) => k.time <= n);
              });
              s.seek(Math.min(s.time, n));
            }}
          />
          <button
            onClick={() =>
              s.edit((sh) => {
                sh.in = Math.min(s.time, sh.out);
              })
            }
          >
            设首帧
          </button>
          <button
            onClick={() =>
              s.edit((sh) => {
                sh.out = Math.max(s.time, sh.in);
                sh.exportRange = "manual";
              })
            }
          >
            设尾帧
          </button>
          <button onClick={() => void edgeCapture()}>
            <Download />
            预演首尾帧
          </button>
        </div>
      </div>
      <div className="timeline-tip">
        {shot.keys.length
          ? `首尾区间 ${shot.in.toFixed(2)}–${shot.out.toFixed(2)}s · 相机键记录机位、视线、焦距 · ${s.autoKey ? "自动打帧已开启" : "修改后点击「打关键帧」保存到当前时刻"}`
          : "运镜：0 秒设置起点并打帧 → 定位到另一秒 → 调整机位 / 朝向 / 焦距并打帧 → 播放。"}
      </div>
      <div className="tracks">
        <div className="track-label">时间 / 秒</div>
        <div className="ruler">
          {Array.from({ length: 9 }, (_, i) => (
            <span key={i}>{((shot.duration * i) / 8).toFixed(1)}</span>
          ))}
          <input
            aria-label="时间轴"
            type="range"
            min={0}
            max={shot.duration}
            step={1 / 24}
            value={s.time}
            onChange={(e) => {
              s.set({ playing: false });
              s.seek(Number(e.target.value));
            }}
          />
        </div>
        {[{ id: "camera", name: "主摄影机" }, ...shot.objects].map((o) => (
          <div className="track-pair" key={o.id}>
            <button
              className="track-label"
              onClick={() => s.set({ selected: o.id })}
            >
              {o.id === "camera" ? <CameraIcon /> : <Box />}
              {o.name}
              <Diamond
                size={11}
                onClick={(e) => {
                  e.stopPropagation();
                  s.key(o.id);
                }}
              />
            </button>
            <div
              className="track"
              onClick={(e) => {
                const r = e.currentTarget.getBoundingClientRect();
                s.set({ playing: false });
                s.seek(((e.clientX - r.left) / r.width) * shot.duration);
              }}
            >
              <div
                className="range"
                style={{
                  left: `${(shot.in / shot.duration) * 100}%`,
                  width: `${((shot.out - shot.in) / shot.duration) * 100}%`,
                }}
              />
              {spatial.越界区间.map((span, idx) => (
                <div
                  key={`oob-${idx}`}
                  className="timeline-uncovered-segment"
                  style={{
                    left: `${(span.from / shot.duration) * 100}%`,
                    width: `${Math.max(0.5, ((span.to - span.from) / shot.duration) * 100)}%`,
                  }}
                  title={`背景越界：${span.from.toFixed(2)}–${span.to.toFixed(2)}s`}
                />
              ))}
              {shot.keys
                .filter((k) => k.target === o.id)
                .map((k) => (
                  <button
                    title={`${k.time.toFixed(3)}s · 点击定位，右键删除`}
                    className="key"
                    key={k.id}
                    style={{ left: `${(k.time / shot.duration) * 100}%` }}
                    onClick={(e) => {
                      e.stopPropagation();
                      s.set({ playing: false });
                      s.seek(k.time);
                    }}
                    onContextMenu={(e) => {
                      e.preventDefault();
                      s.edit(
                        (sh) =>
                          (sh.keys = sh.keys.filter((x) => x.id !== k.id)),
                      );
                    }}
                  >
                    ◆
                  </button>
                ))}
              <div
                className="playhead"
                style={{ left: `${(s.time / shot.duration) * 100}%` }}
              />
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
