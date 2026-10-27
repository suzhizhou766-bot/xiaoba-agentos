import { Box3, Euler, Matrix4, PerspectiveCamera, Quaternion, Ray, Vector3 } from 'three';
import { applyCamera, sampledShot, type Shot, type StageObject, type V3 } from './core';

export type SceneBoard = {
  position: V3;
  width: number;
  height: number;
  yaw: number;
  pitch?: number;
  horizonRatio?: number;
  confirmed: boolean;
};

export const defaultBoard = (aspect = 16 / 9): SceneBoard => ({
  position: [0, 3, -6],
  width: 12,
  height: 12 / aspect,
  yaw: 0,
  pitch: 0,
  horizonRatio: 0.5,
  confirmed: false,
});

export function boardFor(shot: Shot): SceneBoard {
  return shot.sceneBoard ?? defaultBoard();
}

export type OverflowEdges = { left: boolean; right: boolean; top: boolean; bottom: boolean };
export type BoardCoverage = {
  covered: boolean;
  corners: boolean[];
  center: boolean;
  front: boolean;
  overflow: OverflowEdges;
};

export function boardCoverage(shot: Shot): BoardCoverage | null {
  if (!shot.backgroundImageId) return null;
  const board = boardFor(shot),
    camera = applyCamera(new PerspectiveCamera(), shot.camera, shot.objects);
  const pitch = board.pitch ?? 0;
  const inverse = new Matrix4()
    .compose(
      new Vector3(...board.position),
      new Quaternion().setFromEuler(new Euler(pitch, board.yaw, 0, 'YXZ')),
      new Vector3(1, 1, 1),
    )
    .invert();
  const origin = camera.position.clone().applyMatrix4(inverse);
  const samples = [
    [-1, -1],
    [1, -1],
    [1, 1],
    [-1, 1],
    [0, 0],
  ].map(([x, y]) => {
    const point = new Vector3(x, y, 0.5).unproject(camera).applyMatrix4(inverse);
    const dir = point.sub(origin).normalize();
    const t = -origin.z / dir.z;
    const hit = origin.clone().addScaledVector(dir, t);
    const inX = Number.isFinite(t) && t > 0 && Math.abs(hit.x) <= board.width / 2;
    const inY = Number.isFinite(t) && t > 0 && Math.abs(hit.y) <= board.height / 2;
    return { ok: inX && inY, x: hit.x, y: hit.y, t };
  });
  const corners = samples.slice(0, 4);
  const left = corners.some((s) => s.t > 0 && s.x < -board.width / 2);
  const right = corners.some((s) => s.t > 0 && s.x > board.width / 2);
  const bottom = corners.some((s) => s.t > 0 && s.y < -board.height / 2);
  const top = corners.some((s) => s.t > 0 && s.y > board.height / 2);
  return {
    covered: samples.every((s) => s.ok),
    corners: corners.map((s) => s.ok),
    center: samples[4].ok,
    front: origin.z > 0,
    overflow: { left, right, top, bottom },
  };
}

export function objectBox(o: StageObject) {
  return new Box3()
    .setFromCenterAndSize(new Vector3(...o.center), new Vector3(...o.size))
    .applyMatrix4(
      new Matrix4().compose(
        new Vector3(...o.position),
        new Quaternion().setFromEuler(new Euler(...o.rotation)),
        new Vector3(o.scale, o.scale, o.scale),
      ),
    );
}

export function objectBottomY(o: StageObject): number {
  return objectBox(o).min.y;
}

export function groundedPositionY(o: StageObject, targetSurfaceY = 0): number {
  const currentBottom = objectBottomY(o);
  return o.position[1] + (targetSurfaceY - currentBottom);
}

export function spatialFrame(shot: Shot, time: number) {
  const pose = sampledShot(shot, time),
    camera = applyCamera(new PerspectiveCamera(), pose.camera, pose.objects);
  return {
    time,
    coverage: boardCoverage(pose),
    subjects: pose.objects
      .filter((o) => o.visible)
      .map((o) => {
        const box = objectBox(o),
          center = box.getCenter(new Vector3()),
          local = center.clone().applyMatrix4(camera.matrixWorldInverse);
        const points = [box.min.x, box.max.x].flatMap((x) =>
          [box.min.y, box.max.y].flatMap((y) =>
            [box.min.z, box.max.z].map((z) => new Vector3(x, y, z)),
          ),
        );
        const near = points.some(
          (p) => p.clone().applyMatrix4(camera.matrixWorldInverse).z >= -camera.near,
        );
        const proj = points.map((p) => p.project(camera));
        const left = Math.min(...proj.map((p) => p.x)),
          right = Math.max(...proj.map((p) => p.x)),
          top = Math.max(...proj.map((p) => p.y)),
          bottom = Math.min(...proj.map((p) => p.y));
        const outside =
          local.z >= 0 || (!near && (right < -1 || left > 1 || top < -1 || bottom > 1));
        const height = near ? null : Math.round(((top - bottom) / 2) * 1000) / 10;
        const direction = center.clone().sub(camera.position),
          distance = direction.length();
        const ray = new Ray(camera.position, direction.normalize());
        const blockers = pose.objects
          .filter((other) => other.visible && other.id !== o.id)
          .filter((other) => {
            const hit = ray.intersectBox(objectBox(other), new Vector3());
            return hit && hit.distanceTo(camera.position) < distance;
          })
          .map((other) => other.name);
        return {
          id: o.id,
          name: o.name,
          position: o.position,
          depth: Math.round(-local.z * 100) / 100,
          heightPercent: height,
          screen: outside
            ? '画外'
            : near
              ? '穿过近裁切面'
              : left < -1 || right > 1 || top > 1 || bottom < -1
                ? '部分出画'
                : '画内',
          centerBlockers: blockers,
        };
      }),
  };
}

export function heightPercentToShotScale(percent: number | null): { zh: string; en: string } {
  if (percent === null) return { zh: '画外', en: 'out of frame' };
  if (percent < 22) return { zh: '全景/远景', en: 'wide full shot' };
  if (percent < 45) return { zh: '中景', en: 'medium shot' };
  if (percent < 70) return { zh: '中近景', en: 'medium close-up' };
  if (percent <= 100) return { zh: '近景特写', en: 'close-up' };
  return { zh: '大特写/极近景', en: 'extreme close-up' };
}

export function macroCameraMovement(shot: Shot): {
  movement: string;
  movementEn: string;
  subjects: string[];
  cinematicSubjectsZh: string[];
  cinematicSubjectsEn: string[];
} {
  const duration = Math.max(0.1, shot.out - shot.in);
  const keyTimes = shot.keys
    .filter((k) => k.time >= shot.in && k.time <= shot.out)
    .map((k) => k.time);
  const sampleCount = Math.min(24, Math.max(5, Math.round(duration * 4)));
  const gridTimes: number[] = [];
  for (let i = 0; i <= sampleCount; i++) {
    gridTimes.push(shot.in + (duration * i) / sampleCount);
  }
  const allTimes = Array.from(new Set([shot.in, ...keyTimes, ...gridTimes, shot.out])).sort(
    (x, y) => x - y,
  );

  const a = sampledShot(shot, shot.in),
    b = sampledShot(shot, shot.out);
  const camA = applyCamera(new PerspectiveCamera(), a.camera, a.objects);
  const camB = applyCamera(new PerspectiveCamera(), b.camera, b.objects);
  const forward = camA.getWorldDirection(new Vector3());

  const sampledPos: Vector3[] = [];
  const sampledFocals: number[] = [];
  const sampledDepths: number[] = [];
  const sampledTurns: number[] = [];

  for (const t of allTimes) {
    const s = sampledShot(shot, t);
    const pos = new Vector3(...s.camera.position);
    sampledPos.push(pos);
    sampledFocals.push(s.camera.focalLength);
    sampledDepths.push(pos.clone().sub(camA.position).dot(forward));
    const c = applyCamera(new PerspectiveCamera(), s.camera, s.objects);
    sampledTurns.push((camA.quaternion.angleTo(c.quaternion) * 180) / Math.PI);
  }

  const delta = new Vector3(...b.camera.position).sub(new Vector3(...a.camera.position));
  const netDepth = delta.dot(forward);
  const lateralVec = delta.clone().addScaledVector(forward, -netDepth);
  const lateral = lateralVec.length();
  const netTurn = (camA.quaternion.angleTo(camB.quaternion) * 180) / Math.PI;
  const maxTurn = Math.max(...sampledTurns);

  const minFocal = Math.min(...sampledFocals);
  const maxFocal = Math.max(...sampledFocals);
  const startFocal = a.camera.focalLength;
  const endFocal = b.camera.focalLength;
  const focalRange = maxFocal - minFocal;

  const minDepth = Math.min(...sampledDepths);
  const maxDepth = Math.max(...sampledDepths);
  const depthRange = maxDepth - minDepth;

  const maxDisplacement = Math.max(...sampledPos.map((p) => p.distanceTo(camA.position)));

  const parts: string[] = [];
  const partsEn: string[] = [];

  if (maxDisplacement < 0.05 && maxTurn < 0.5 && focalRange < 0.2 && depthRange < 0.05) {
    parts.push('机位固定（Static Shot），构图稳定无位移');
    partsEn.push('Static locked camera, maintaining stable framing with zero displacement');
  } else {
    // Dolly / Depth analysis
    if (Math.abs(netDepth) < 0.08 && depthRange >= 0.2) {
      if (maxDepth >= 0.15) {
        parts.push(`向前推进约 ${maxDepth.toFixed(1)}m 后拉回原位（Dolly In & Out）`);
        partsEn.push(`Smooth Dolly In ~${maxDepth.toFixed(1)}m then pulling backward to initial depth`);
      } else if (minDepth <= -0.15) {
        parts.push(`向后拉远约 ${Math.abs(minDepth).toFixed(1)}m 后推进回位（Dolly Out & In）`);
        partsEn.push(`Smooth Dolly Out ~${Math.abs(minDepth).toFixed(1)}m then pushing forward to initial depth`);
      }
    } else if (Math.abs(netDepth) >= 0.08) {
      parts.push(
        netDepth > 0
          ? `向前推进（Dolly In）约 ${netDepth.toFixed(1)}m`
          : `向后拉远（Dolly Out）约 ${Math.abs(netDepth).toFixed(1)}m`,
      );
      partsEn.push(
        netDepth > 0
          ? `Smooth Dolly In pushing forward ~${netDepth.toFixed(1)}m`
          : `Smooth Dolly Out pulling backward ~${Math.abs(netDepth).toFixed(1)}m`,
      );
    }

    if (lateral >= 0.08) {
      parts.push(`伴随横向/升降位移（Truck/Pedestal）约 ${lateral.toFixed(1)}m`);
      partsEn.push(`subtle lateral tracking & pedestal shift (~${lateral.toFixed(1)}m)`);
    }

    if (netTurn < 1.0 && maxTurn >= 3.0) {
      parts.push(`机位大幅摇镜转向约 ${maxTurn.toFixed(1)}° 后平滑回正（Pan & Return）`);
      partsEn.push(`smooth pan/tilt sweep ~${maxTurn.toFixed(1)}° and returning to initial aim (Pan & Return)`);
    } else if (netTurn >= 1.0) {
      parts.push(`机位朝向/俯仰环绕变化约 ${netTurn.toFixed(1)}°`);
      partsEn.push(`smooth pan/tilt rotation adjust (~${netTurn.toFixed(1)}°)`);
    }

    // Focal length / Zoom analysis
    if (Math.abs(endFocal - startFocal) < 1.0 && focalRange >= 4.0) {
      if (maxFocal - startFocal >= 4.0) {
        parts.push(`光学变焦推进至 ${maxFocal.toFixed(0)}mm 后回缩至 ${endFocal.toFixed(0)}mm`);
        partsEn.push(`optical zoom in to ${maxFocal.toFixed(0)}mm then pulling back to ${endFocal.toFixed(0)}mm`);
      } else if (startFocal - minFocal >= 4.0) {
        parts.push(`光学变焦拉远至 ${minFocal.toFixed(0)}mm 后推进至 ${endFocal.toFixed(0)}mm`);
        partsEn.push(`optical zoom out to ${minFocal.toFixed(0)}mm then pushing in to ${endFocal.toFixed(0)}mm`);
      }
    } else if (Math.abs(endFocal - startFocal) >= 1.0) {
      parts.push(`焦距由 ${startFocal.toFixed(0)}mm 变焦至 ${endFocal.toFixed(0)}mm`);
      partsEn.push(`optical zoom from ${startFocal.toFixed(0)}mm to ${endFocal.toFixed(0)}mm`);
    }
  }

  const movement = parts.join('，') || '机位保持平缓微动';
  const movementEn = partsEn.join(', ') || 'Smooth steady camera with subtle natural motion';

  const startF = spatialFrame(shot, shot.in),
    endF = spatialFrame(shot, shot.out);
  const subjects = startF.subjects.map((s) => {
    const endS = endF.subjects.find((x) => x.id === s.id);
    if (!endS) return `${s.name}：在场`;
    const startH = s.heightPercent !== null ? `高度占比 ${s.heightPercent}%` : '';
    const endH = endS.heightPercent !== null ? `高度占比 ${endS.heightPercent}%` : '';
    const hChange = startH && endH && startH !== endH ? `（${startH} → ${endH}）` : startH ? `（${startH}）` : '';
    return `${s.name}：起帧${s.screen}，尾帧${endS.screen}${hChange}`;
  });

  const cinematicSubjectsZh: string[] = [];
  const cinematicSubjectsEn: string[] = [];

  for (const s of startF.subjects) {
    const endS = endF.subjects.find((x) => x.id === s.id);
    const startScale = heightPercentToShotScale(s.heightPercent);
    const endScale = heightPercentToShotScale(endS ? endS.heightPercent : null);
    const cleanNameZh = s.name.replace(/^角色\s*[A-Z]\s*·\s*/, '') || s.name;
    const cleanNameEn = s.name.includes('主角')
      ? 'The protagonist'
      : s.name.includes('对手')
      ? 'The secondary subject'
      : cleanNameZh;

    const subjHeights: number[] = [];
    for (const t of allTimes) {
      const sf = spatialFrame(shot, t);
      const targetSubj = sf.subjects.find((x) => x.id === s.id);
      if (targetSubj && targetSubj.heightPercent !== null && targetSubj.screen !== '画外') {
        subjHeights.push(targetSubj.heightPercent);
      }
    }
    const minSubjH = subjHeights.length ? Math.min(...subjHeights) : (s.heightPercent ?? 0);
    const maxSubjH = subjHeights.length ? Math.max(...subjHeights) : (s.heightPercent ?? 0);
    const startSubjH = s.heightPercent ?? minSubjH;

    if (!endS || endS.screen === '画外') {
      const actorMoved = (() => {
        const startActor = a.objects.find((o) => o.id === s.id);
        const endActor = b.objects.find((o) => o.id === s.id);
        if (startActor && endActor) {
          const dx = endActor.position[0] - startActor.position[0];
          const dy = endActor.position[1] - startActor.position[1];
          const dz = endActor.position[2] - startActor.position[2];
          return Math.sqrt(dx * dx + dy * dy + dz * dz) >= 0.25;
        }
        return false;
      })();

      const cameraPanned = maxTurn >= 1.5;
      const cameraPushed = netDepth >= 0.2 || maxDepth >= 0.3;

      if (actorMoved && !cameraPanned && !cameraPushed) {
        cinematicSubjectsZh.push(`${s.name}：起帧呈${startScale.zh}，角色自行走位移出画面`);
        cinematicSubjectsEn.push(`${cleanNameEn} begins in ${startScale.en}, then naturally exits the frame as the actor moves`);
      } else if (cameraPanned && !cameraPushed) {
        cinematicSubjectsZh.push(`${s.name}：起帧呈${startScale.zh}，随运镜摇镜转向移出画面`);
        cinematicSubjectsEn.push(`${cleanNameEn} begins in ${startScale.en}, then naturally exits the frame as camera pans away`);
      } else if (cameraPushed) {
        cinematicSubjectsZh.push(`${s.name}：起帧呈${startScale.zh}，随运镜向前推移自然出画`);
        cinematicSubjectsEn.push(`${cleanNameEn} begins in ${startScale.en}, then naturally exits the frame as camera pushes forward`);
      } else {
        cinematicSubjectsZh.push(`${s.name}：起帧呈${startScale.zh}，尾帧移出画面`);
        cinematicSubjectsEn.push(`${cleanNameEn} begins in ${startScale.en}, then naturally exits the frame`);
      }
    } else if (startScale.zh === endScale.zh) {
      if (maxSubjH - minSubjH >= 20) {
        if (maxSubjH - startSubjH >= 20) {
          const peakScale = heightPercentToShotScale(maxSubjH);
          cinematicSubjectsZh.push(`${s.name}：起帧呈${startScale.zh}，随运镜景别收窄至${peakScale.zh}后拉回${endScale.zh}`);
          cinematicSubjectsEn.push(`${cleanNameEn} begins in ${startScale.en}, frames tighter into ${peakScale.en}, then pulls back to ${endScale.en}`);
        } else if (startSubjH - minSubjH >= 20) {
          const wideScale = heightPercentToShotScale(minSubjH);
          cinematicSubjectsZh.push(`${s.name}：起帧呈${startScale.zh}，随运镜景别放宽至${wideScale.zh}后回推至${endScale.zh}`);
          cinematicSubjectsEn.push(`${cleanNameEn} begins in ${startScale.en}, widens out to ${wideScale.en}, then pushes back to ${endScale.en}`);
        } else {
          cinematicSubjectsZh.push(`${s.name}：全程保持${startScale.zh}构图`);
          cinematicSubjectsEn.push(`${cleanNameEn} maintains steady ${startScale.en} framing throughout`);
        }
      } else {
        cinematicSubjectsZh.push(`${s.name}：全程保持${startScale.zh}构图`);
        cinematicSubjectsEn.push(`${cleanNameEn} maintains steady ${startScale.en} framing throughout`);
      }
    } else {
      cinematicSubjectsZh.push(`${s.name}：从起帧${startScale.zh}平滑过渡推进至尾帧${endScale.zh}`);
      cinematicSubjectsEn.push(`${cleanNameEn} smoothly transitions from ${startScale.en} into ${endScale.en}`);
    }
  }

  return { movement, movementEn, subjects, cinematicSubjectsZh, cinematicSubjectsEn };
}

export function spatialReport(shot: Shot) {
  const times = [
    ...new Set([
      shot.in,
      shot.out,
      ...shot.keys.map((k) => k.time).filter((t) => t > shot.in && t < shot.out),
      ...Array.from({ length: Math.ceil((shot.out - shot.in) * 2) }, (_, i) =>
        Math.min(shot.out, shot.in + i / 2),
      ),
    ]),
  ].sort((a, b) => a - b);
  const frames = times.map((t) => spatialFrame(shot, t));
  const narrative = frames.slice(1).map((frame, i) => {
    const a = sampledShot(shot, frames[i].time),
      b = sampledShot(shot, frame.time);
    const delta = new Vector3(...b.camera.position).sub(new Vector3(...a.camera.position));
    const camera = applyCamera(new PerspectiveCamera(), a.camera, a.objects),
      forward = camera.getWorldDirection(new Vector3());
    const depth = delta.dot(forward),
      lateral = delta.clone().addScaledVector(forward, -depth).length();
    const focal = b.camera.focalLength - a.camera.focalLength;
    const endCamera = applyCamera(new PerspectiveCamera(), b.camera, b.objects);
    const turn = (camera.quaternion.angleTo(endCamera.quaternion) * 180) / Math.PI;
    const movement =
      delta.length() < 0.001
        ? '机位固定'
        : [
            Math.abs(depth) > 0.001 ? (depth > 0 ? '沿镜头方向推进' : '沿镜头方向后退') : '',
            lateral > 0.001 ? '伴随横向或升降位移' : '',
          ]
            .filter(Boolean)
            .join('，');
    const subjects = frame.subjects
      .map((o) => {
        const prev = frames[i].subjects.find((x) => x.id === o.id)!;
        return `${o.name}：${prev.screen}至${o.screen}${prev.heightPercent !== null && o.heightPercent !== null ? `，投影高度从画面 ${prev.heightPercent}% 变为 ${o.heightPercent}%` : ''}`;
      })
      .join('；');
    return {
      开始秒: frames[i].time,
      结束秒: frame.time,
      描述: `${movement}；${turn > 0.1 ? `朝向/滚转变化约 ${turn.toFixed(1)}°` : '朝向保持'}；${Math.abs(focal) > 0.01 ? `焦距由 ${a.camera.focalLength.toFixed(1)}mm 变为 ${b.camera.focalLength.toFixed(1)}mm` : '焦距保持'}。${subjects}。`,
    };
  });
  const changes = frames.slice(1).flatMap((frame, i) =>
    frame.subjects.flatMap((subject) => {
      const previous = frames[i].subjects.find((s) => s.id === subject.id);
      if (!previous) return [];
      const notes: string[] = [];
      if (previous.screen !== subject.screen) notes.push(`${previous.screen} → ${subject.screen}`);
      if (
        previous.heightPercent !== null &&
        subject.heightPercent !== null &&
        Math.abs(subject.heightPercent - previous.heightPercent) >= 2
      )
        notes.push(`投影高度 ${previous.heightPercent}% → ${subject.heightPercent}%`);
      if (previous.centerBlockers.join() !== subject.centerBlockers.join())
        notes.push(`中心视线遮挡候选：${subject.centerBlockers.join('、') || '无'}`);
      return notes.length ? [{ from: frames[i].time, to: frame.time, subject: subject.name, notes }] : [];
    }),
  );
  const uncovered: { from: number; to: number }[] = [];
  if (shot.backgroundImageId)
    for (let f = Math.ceil(shot.in * 24); f <= Math.floor(shot.out * 24); f++) {
      const t = f / 24,
        bad = !boardCoverage(sampledShot(shot, t))?.covered;
      if (bad) {
        const last = uncovered.at(-1);
        if (last && t - last.to < 1.5 / 24) last.to = t;
        else uncovered.push({ from: t, to: t });
      }
    }
  const macro = macroCameraMovement(shot);
  return {
    空间依据: shot.sceneBoard?.confirmed
      ? '用户确认的简化空间'
      : '待用户校准的简化空间，不能视为图片真实深度',
    背景板: shot.backgroundImageId ? boardFor(shot) : null,
    越界区间: uncovered,
    宏观运镜总结: macro.movement,
    宏观运镜总结En: macro.movementEn,
    宏观主体动态: macro.subjects,
    影视主体动态Zh: macro.cinematicSubjectsZh,
    影视主体动态En: macro.cinematicSubjectsEn,
    运镜与画面描述: narrative,
    构图变化: changes,
    空间采样: frames,
    检查范围:
      '背景板按 24fps 检查；主体每 0.5 秒及关键帧采样。画面高度基于包围盒，不代表自动识别人脸景别。遮挡为包围盒中心射线候选，实际外观以监看为准。',
  };
}
