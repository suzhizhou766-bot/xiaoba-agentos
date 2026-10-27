import {
  useEffect,
  useRef,
  useState,
  Component,
  type ReactNode,
} from "react";
import { type V3 } from "./core";
export function download(name: string, data: string, type = "application/json") {
  const a = document.createElement("a");
  const url = data.startsWith("data:image/")
    ? data
    : URL.createObjectURL(new Blob([data], { type }));
  a.href = url;
  a.download = name;
  a.click();
  if (url.startsWith("blob:")) setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export class Boundary extends Component<{ children: ReactNode }, { error: string }> {
  state = { error: "" };
  static getDerivedStateFromError(e: Error) {
    return { error: e.message };
  }
  render() {
    return this.state.error ? (
      <div className="error">
        3D 视口无法启动：{this.state.error}
        <br />
        请使用支持 WebGL 的浏览器并刷新。
      </div>
    ) : (
      this.props.children
    );
  }
}
export function Field({
  label,
  value,
  onChange,
  min = -1000,
  max = 1000,
  step = 0.1,
  unit = "",
  ariaLabel,
}: {
  label: string;
  value: number;
  onChange: (n: number) => void;
  min?: number;
  max?: number;
  step?: number;
  unit?: string;
  ariaLabel?: string;
}) {
  const [draft, setDraft] = useState(String(Number(value.toFixed(3))));
  const focused = useRef(false);
  useEffect(() => {
    if (!focused.current) setDraft(String(Number(value.toFixed(3))));
  }, [value]);
  const finish = () => {
    focused.current = false;
    const n = Number(draft);
    if (draft.trim() && Number.isFinite(n)) {
      const next = Math.min(max, Math.max(min, n));
      onChange(next);
      setDraft(String(Number(next.toFixed(3))));
    } else setDraft(String(Number(value.toFixed(3))));
  };
  return (
    <label className="field">
      <span>{label}</span>
      <div>
        <input
          aria-label={ariaLabel ?? label}
          type="number"
          value={draft}
          onFocus={() => {
            focused.current = true;
          }}
          onBlur={finish}
          onKeyDown={(e) => {
            if (e.key === "Enter") e.currentTarget.blur();
          }}
          min={min}
          max={max}
          step={step}
          onChange={(e) => {
            setDraft(e.target.value);
            const n = e.target.valueAsNumber;
            if (Number.isFinite(n) && n >= min && n <= max) onChange(n);
          }}
        />
        <small>{unit}</small>
      </div>
    </label>
  );
}
export function Vector({
  label,
  value,
  onChange,
}: {
  label: string;
  value: V3;
  onChange: (v: V3) => void;
}) {
  return (
    <div className="vector">
      <label>{label}</label>
      <div>
        {value.map((v, i) => (
          <Field
            key={i}
            label={["X", "Y", "Z"][i]}
            ariaLabel={`${label} ${["X", "Y", "Z"][i]}`}
            value={v}
            onChange={(n) =>
              onChange(value.map((x, j) => (j === i ? n : x)) as V3)
            }
          />
        ))}
      </div>
    </div>
  );
}
