import React, { useState, useMemo } from "react";
import {
  azimuthToDirectionLabel,
  defaultLighting,
  describeLighting,
  lightingPresets,
  type LightingConfig,
  type LightingPresetId,
} from "./core";
import { active, useStore } from "./store";

const COLOR_PRESETS = [
  { label: "暖白 4500K", color: "#fff4e6" },
  { label: "影视日光 5600K", color: "#ffffff" },
  { label: "冷白 6500K", color: "#f0f5ff" },
  { label: "暖金夕阳 2800K", color: "#ff9d42" },
  { label: "月光青蓝 8000K", color: "#a8d2f7" },
];

const FILL_COLOR_PRESETS = [
  { label: "柔冷调补光", color: "#b8d5f8" },
  { label: "漫射正白", color: "#ffffff" },
  { label: "暮色青蓝", color: "#7c8eb5" },
  { label: "深钢冷调", color: "#4a6080" },
];

const RIM_COLOR_PRESETS = [
  { label: "通透正白", color: "#ffffff" },
  { label: "金黄发丝", color: "#ffb469" },
  { label: "银青冷月", color: "#d8ebff" },
];

export function LightingPanel() {
  const state = useStore();
  const shot = active(state.project);
  const lighting = shot.lighting ?? defaultLighting();
  const [copied, setCopied] = useState(false);

  const update = (patch: Partial<LightingConfig>, markCustom = true) => {
    state.edit((s) => {
      const current = s.lighting ?? defaultLighting();
      s.lighting = {
        ...current,
        ...patch,
        preset: markCustom ? "custom" : patch.preset ?? current.preset,
      };
    }, false);
  };

  const applyPreset = (presetId: LightingPresetId) => {
    const p = lightingPresets[presetId];
    if (!p) return;
    state.edit((s) => {
      s.lighting = {
        preset: presetId,
        ...p.config,
      };
    }, true);
    state.set({ notice: `已应用布光预设：【${p.name}】（${p.tagline}）` });
  };

  const lightingDesc = useMemo(
    () => describeLighting(lighting),
    [lighting],
  );

  const keyDir = azimuthToDirectionLabel(lighting.keyAzimuth);
  const fillDir = azimuthToDirectionLabel(lighting.fillAzimuth);
  const rimDir = azimuthToDirectionLabel(lighting.rimAzimuth);

  const ratioVal =
    lighting.fillEnabled && lighting.fillIntensity > 0
      ? (lighting.keyIntensity / lighting.fillIntensity).toFixed(1)
      : null;

  const copyPrompt = async () => {
    try {
      if (!navigator?.clipboard?.writeText) {
        throw new Error("当前环境不支持剪贴板写入");
      }
      await navigator.clipboard.writeText(lightingDesc.detailZh);
      setCopied(true);
      state.set({ notice: "已将布光提示词复制到剪贴板，可直接粘贴到视频大模型中" });
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      state.set({ notice: "复制失败：" + ((err as Error).message || "权限不足") });
    }
  };

  return (
    <div className="lighting-panel">
      {/* 预设选择区 */}
      <div className="section-title">
        PRESETS <span>电影布光预设</span>
      </div>
      <div className="preset-grid">
        {(Object.keys(lightingPresets) as LightingPresetId[])
          .filter((id) => id !== "custom")
          .map((id) => {
            const p = lightingPresets[id];
            const active = lighting.preset === id;
            return (
              <button
                key={id}
                type="button"
                className={`preset-btn ${active ? "active" : ""}`}
                onClick={() => applyPreset(id)}
                title={p.description}
              >
                <div className="preset-name">{p.name}</div>
                <div className="preset-tagline">{p.tagline}</div>
              </button>
            );
          })}
      </div>

      {/* 主光控制 */}
      <div className="section-title">
        KEY LIGHT <span>主光源 · 塑形与主阴影</span>
      </div>
      <div className="lighting-group">
        <div className="slider-row">
          <div className="slider-label">
            <span>水平方位角</span>
            <span className="badge-pill direction-badge">{keyDir.zh} ({lighting.keyAzimuth}°)</span>
          </div>
          <input
            type="range"
            min={0}
            max={360}
            step={1}
            value={lighting.keyAzimuth}
            onChange={(e) => update({ keyAzimuth: Number(e.target.value) })}
            onPointerUp={() => state.commitHistory()}
          />
        </div>

        <div className="slider-row">
          <div className="slider-label">
            <span>俯仰高度角</span>
            <span className="badge-pill">{lighting.keyElevation}°</span>
          </div>
          <input
            type="range"
            min={10}
            max={85}
            step={1}
            value={lighting.keyElevation}
            onChange={(e) => update({ keyElevation: Number(e.target.value) })}
            onPointerUp={() => state.commitHistory()}
          />
        </div>

        <div className="slider-row">
          <div className="slider-label">
            <span>主光强度</span>
            <span className="badge-pill">{lighting.keyIntensity.toFixed(1)}</span>
          </div>
          <input
            type="range"
            min={0.2}
            max={5.0}
            step={0.1}
            value={lighting.keyIntensity}
            onChange={(e) => update({ keyIntensity: Number(e.target.value) })}
            onPointerUp={() => state.commitHistory()}
          />
        </div>

        <div className="color-palette-row">
          <span className="color-row-title">光照色温</span>
          <div className="color-presets">
            {COLOR_PRESETS.map((c) => (
              <button
                key={c.color}
                type="button"
                className={`color-pill ${lighting.keyColor.toLowerCase() === c.color.toLowerCase() ? "active" : ""}`}
                style={{ backgroundColor: c.color }}
                title={c.label}
                onClick={() => {
                  update({ keyColor: c.color });
                  state.commitHistory();
                }}
              />
            ))}
            <input
              type="color"
              className="color-picker-input"
              value={lighting.keyColor}
              title="自定义主光颜色"
              onChange={(e) => update({ keyColor: e.target.value })}
              onBlur={() => state.commitHistory()}
            />
          </div>
        </div>
      </div>

      {/* 辅光/补光控制 */}
      <div className="section-title toggleable-title">
        <div className="title-left">
          FILL LIGHT <span>辅光 · 暗部补光与光比</span>
        </div>
        <label className="switch-toggle" title="开关辅光">
          <input
            type="checkbox"
            checked={lighting.fillEnabled}
            onChange={(e) => {
              update({ fillEnabled: e.target.checked });
              state.commitHistory();
            }}
          />
          <span className="slider round" />
        </label>
      </div>

      {lighting.fillEnabled ? (
        <div className="lighting-group">
          <div className="ratio-status-banner">
            <span className="ratio-label">实时主辅光比：</span>
            <span className="ratio-highlight">
              {ratioVal ? `1:${ratioVal}` : "高反差"}
            </span>
            <span className="ratio-hint">
              {ratioVal && Number(ratioVal) <= 2.5
                ? "（柔和自然·低反差）"
                : ratioVal && Number(ratioVal) <= 4.5
                ? "（经典电影·立体适中）"
                : "（高戏剧感·硬朗反差）"}
            </span>
          </div>

          <div className="slider-row">
            <div className="slider-label">
              <span>辅光强度</span>
              <span className="badge-pill">{lighting.fillIntensity.toFixed(2)}</span>
            </div>
            <input
              type="range"
              min={0.0}
              max={3.0}
              step={0.05}
              value={lighting.fillIntensity}
              onChange={(e) => update({ fillIntensity: Number(e.target.value) })}
              onPointerUp={() => state.commitHistory()}
            />
          </div>

          <div className="slider-row">
            <div className="slider-label">
              <span>辅光方位角</span>
              <span className="badge-pill direction-badge">{fillDir.zh} ({lighting.fillAzimuth}°)</span>
            </div>
            <input
              type="range"
              min={0}
              max={360}
              step={1}
              value={lighting.fillAzimuth}
              onChange={(e) => update({ fillAzimuth: Number(e.target.value) })}
              onPointerUp={() => state.commitHistory()}
            />
          </div>

          <div className="slider-row">
            <div className="slider-label">
              <span>辅光俯仰角</span>
              <span className="badge-pill">{lighting.fillElevation}°</span>
            </div>
            <input
              type="range"
              min={10}
              max={80}
              step={1}
              value={lighting.fillElevation}
              onChange={(e) => update({ fillElevation: Number(e.target.value) })}
              onPointerUp={() => state.commitHistory()}
            />
          </div>

          <div className="color-palette-row">
            <span className="color-row-title">补光色调</span>
            <div className="color-presets">
              {FILL_COLOR_PRESETS.map((c) => (
                <button
                  key={c.color}
                  type="button"
                  className={`color-pill ${lighting.fillColor.toLowerCase() === c.color.toLowerCase() ? "active" : ""}`}
                  style={{ backgroundColor: c.color }}
                  title={c.label}
                  onClick={() => {
                    update({ fillColor: c.color });
                    state.commitHistory();
                  }}
                />
              ))}
              <input
                type="color"
                className="color-picker-input"
                value={lighting.fillColor}
                title="自定义辅光颜色"
                onChange={(e) => update({ fillColor: e.target.value })}
                onBlur={() => state.commitHistory()}
              />
            </div>
          </div>
        </div>
      ) : (
        <div className="disabled-hint">辅光已关闭：保留纯粹硬光与深邃阴影</div>
      )}

      {/* 轮廓光控制 */}
      <div className="section-title toggleable-title">
        <div className="title-left">
          RIM LIGHT <span>轮廓光 · 发丝与边缘剥离</span>
        </div>
        <label className="switch-toggle" title="开关轮廓光">
          <input
            type="checkbox"
            checked={lighting.rimEnabled}
            onChange={(e) => {
              update({ rimEnabled: e.target.checked });
              state.commitHistory();
            }}
          />
          <span className="slider round" />
        </label>
      </div>

      {lighting.rimEnabled ? (
        <div className="lighting-group">
          <div className="slider-row">
            <div className="slider-label">
              <span>轮廓光强度</span>
              <span className="badge-pill">{lighting.rimIntensity.toFixed(1)}</span>
            </div>
            <input
              type="range"
              min={0.0}
              max={4.0}
              step={0.1}
              value={lighting.rimIntensity}
              onChange={(e) => update({ rimIntensity: Number(e.target.value) })}
              onPointerUp={() => state.commitHistory()}
            />
          </div>

          <div className="slider-row">
            <div className="slider-label">
              <span>轮廓方位角</span>
              <span className="badge-pill direction-badge">{rimDir.zh} ({lighting.rimAzimuth}°)</span>
            </div>
            <input
              type="range"
              min={0}
              max={360}
              step={1}
              value={lighting.rimAzimuth}
              onChange={(e) => update({ rimAzimuth: Number(e.target.value) })}
              onPointerUp={() => state.commitHistory()}
            />
          </div>

          <div className="slider-row">
            <div className="slider-label">
              <span>轮廓俯仰角</span>
              <span className="badge-pill">{lighting.rimElevation}°</span>
            </div>
            <input
              type="range"
              min={15}
              max={85}
              step={1}
              value={lighting.rimElevation}
              onChange={(e) => update({ rimElevation: Number(e.target.value) })}
              onPointerUp={() => state.commitHistory()}
            />
          </div>

          <div className="color-palette-row">
            <span className="color-row-title">轮廓光色</span>
            <div className="color-presets">
              {RIM_COLOR_PRESETS.map((c) => (
                <button
                  key={c.color}
                  type="button"
                  className={`color-pill ${lighting.rimColor.toLowerCase() === c.color.toLowerCase() ? "active" : ""}`}
                  style={{ backgroundColor: c.color }}
                  title={c.label}
                  onClick={() => {
                    update({ rimColor: c.color });
                    state.commitHistory();
                  }}
                />
              ))}
              <input
                type="color"
                className="color-picker-input"
                value={lighting.rimColor}
                title="自定义轮廓光颜色"
                onChange={(e) => update({ rimColor: e.target.value })}
                onBlur={() => state.commitHistory()}
              />
            </div>
          </div>
        </div>
      ) : (
        <div className="disabled-hint">轮廓光已关闭：无发丝边缘高光分离</div>
      )}

      {/* 环境光调节 */}
      <div className="section-title">
        AMBIENT <span>环境漫射与地面反射</span>
      </div>
      <div className="lighting-group">
        <div className="slider-row">
          <div className="slider-label">
            <span>环境漫射光强</span>
            <span className="badge-pill">{lighting.ambientIntensity.toFixed(2)}</span>
          </div>
          <input
            type="range"
            min={0.05}
            max={1.5}
            step={0.05}
            value={lighting.ambientIntensity}
            onChange={(e) => update({ ambientIntensity: Number(e.target.value) })}
            onPointerUp={() => state.commitHistory()}
          />
        </div>

        <div className="slider-row">
          <div className="slider-label">
            <span>地面反光强度</span>
            <span className="badge-pill">{(lighting.groundBounceIntensity ?? 0.35).toFixed(2)}</span>
          </div>
          <input
            type="range"
            min={0.0}
            max={1.2}
            step={0.05}
            value={lighting.groundBounceIntensity ?? 0.35}
            onChange={(e) => update({ groundBounceIntensity: Number(e.target.value) })}
            onPointerUp={() => state.commitHistory()}
          />
        </div>
      </div>

      {/* 提示词实时生成预览与复制卡片 */}
      <div className="section-title">
        PROMPT SYNTHESIS <span>大模型提示词输出</span>
      </div>
      <div className="prompt-preview-card">
        <div className="prompt-preview-header">
          <span className="prompt-tag">AI 视频/图像模型布光段落</span>
          <button
            type="button"
            className={`copy-prompt-btn ${copied ? "copied" : ""}`}
            onClick={copyPrompt}
          >
            {copied ? "已复制 √" : "复制布光描述"}
          </button>
        </div>
        <div className="prompt-preview-content">
          {lightingDesc.detailZh}
        </div>
        <div className="prompt-preview-en">
          {lightingDesc.detailEn}
        </div>
      </div>
    </div>
  );
}
