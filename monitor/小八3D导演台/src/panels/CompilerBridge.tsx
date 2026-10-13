import { useEffect, useState } from "react";
import { Send, Sparkles } from "lucide-react";
import type { Project, Shot } from "../core";
import {
  compose,
  composeInfo,
  sendToDirector,
  toDirectorParams,
  type ComposeInfo,
  type ComposeResult,
} from "../directorBridge";

/** 导演编译器里与监看中心其它部分打通的两个动作。 */
export function CompilerBridge({
  project,
  shot,
  notify,
}: {
  project: Project;
  shot: Shot;
  notify: (text: string) => void;
}) {
  const [info, setInfo] = useState<ComposeInfo | null>(null);
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ComposeResult | null>(null);
  const [error, setError] = useState("");
  const hosted = location.pathname.startsWith("/stage");

  useEffect(() => {
    if (!hosted) return;
    let alive = true;
    void composeInfo().then((next) => {
      if (!alive) return;
      setInfo(next);
      setModel((current) => current || next.models[0] || "tierflow_pro");
    });
    return () => {
      alive = false;
    };
  }, [hosted]);
  // 换了镜头：上一个镜头的结果不再对应
  useEffect(() => {
    setResult(null);
    setError("");
  }, [shot.id]);

  const send = () => {
    try {
      const handoff = toDirectorParams(project, shot);
      const how = sendToDirector(handoff);
      notify(
        how === "switched"
          ? `已把镜头《${handoff.shot}》送到提示词导演台`
          : `已送出镜头《${handoff.shot}》，打开监看中心的「导演台」标签即可看到`,
      );
    } catch (err) {
      notify("送出失败：" + ((err as Error).message || "浏览器不允许写入本地存储"));
    }
  };
  const run = async () => {
    setBusy(true);
    setError("");
    try {
      setResult(await compose(project, shot, model.trim()));
    } catch (err) {
      setError((err as Error).message || "生成失败");
    } finally {
      setBusy(false);
    }
  };
  const who = !info
    ? "正在读取 Key 池…"
    : !info.available
      ? info.reason || "Key 池不可用"
      : info.followsClient
        ? "Key 池里有节点跟随这里填的模型名"
        : `由 Key 池节点设定的模型来写：${info.models.join("、")}`;

  if (!hosted) return null;
  return (
    <fieldset className="model-picker bridge">
      <legend>接到监看中心</legend>
      <div className="bridge-row">
        <button onClick={send} title="把景别、焦距、光圈、机位、运镜、灯光、人数填进提示词导演台的参数面板">
          <Send />
          <span>送到提示词导演台</span>
        </button>
        <small>只在本机两个页面之间传参数，不联网、不花钱。</small>
      </div>
      <div className="bridge-row">
        <button className="gold" disabled={busy || !shot.videoModel || !info?.available || !model.trim()} onClick={run}>
          <Sparkles />
          <span>{busy ? "模型正在写…" : result ? "重新生成" : "用 Key 池模型写最终提示词"}</span>
        </button>
        {info?.available && info.followsClient && (
          <input
            aria-label="模型名称"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder="模型名称"
            list="bridge-models"
          />
        )}
        <datalist id="bridge-models">
          {info?.models.map((m) => <option key={m} value={m} />)}
        </datalist>
        <small>
          {!shot.videoModel ? "先在下面勾选视频模型。" : who}
          {" "}每点一次发一个真实请求，会消耗 Key 池额度；慢的时候要等一两分钟。
        </small>
      </div>
      {error && <p className="coverage-warning">生成失败：{error}</p>}
      {result && (
        <>
          <textarea aria-label="最终提示词" readOnly value={result.text} />
          <div className="bridge-row">
            <button
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(result.text);
                  notify("已复制最终提示词");
                } catch {
                  notify("复制失败：浏览器不允许写入剪贴板");
                }
              }}
            >
              复制
            </button>
            <small>
              {result.model} · 用时 {(result.ms / 1000).toFixed(1)} 秒 · 这是模型写的草稿，角色外观和首尾帧仍要你对照参考素材核对。
            </small>
          </div>
        </>
      )}
    </fieldset>
  );
}
