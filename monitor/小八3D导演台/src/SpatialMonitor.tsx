import { active, useStore } from './store';
import { spatialFrame } from './spatial';

export function SpatialMonitor() {
  const s = useStore(),
    shot = active(s.project),
    frame = spatialFrame(shot, s.time);
  const edges: string[] = [];
  if (frame.coverage && !frame.coverage.covered && frame.coverage.overflow) {
    if (frame.coverage.overflow.left) edges.push('左侧');
    if (frame.coverage.overflow.right) edges.push('右侧');
    if (frame.coverage.overflow.top) edges.push('上方');
    if (frame.coverage.overflow.bottom) edges.push('下方');
  }
  const edgeDesc = edges.length ? `（${edges.join('、')}穿帮）` : '';

  return (
    <details className="spatial-monitor" open>
      <summary>实时空间反馈</summary>
      {frame.coverage && (
        <p className={!frame.coverage.covered ? 'coverage-warning' : ''}>
          {frame.coverage.covered
            ? '背景板覆盖完整'
            : `⚠ 取景超出背景板${edgeDesc}，需补充素材或调整运镜`}
          {!frame.coverage.front ? ' · 正在拍摄背景板背面' : ''}
        </p>
      )}
      {frame.subjects.map((o) => (
        <p key={o.id}>
          {o.name} · {o.screen} · 纵深 {o.depth}m
          {o.heightPercent !== null ? ` · 投影高度 ${o.heightPercent}%` : ''}
          {o.centerBlockers.length
            ? ` · 中心视线可能被 ${o.centerBlockers.join('、')} 遮挡`
            : ''}
        </p>
      ))}
      <small>高度与遮挡是包围盒近似；最终可见范围以监看为准。</small>
    </details>
  );
}

