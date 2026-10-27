import { boardFor, groundedPositionY } from "./spatial";
import { hipHeight, poseOf, RIG, type PoseId } from "./poses";
import { imageTextures } from "./sceneImages";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import {
  Line,
  Grid,
  OrbitControls,
  TransformControls,
  GizmoHelper,
  GizmoViewport,
} from "@react-three/drei";
import { DoubleSide, Group, PerspectiveCamera, Vector3, Vector2, Euler, BufferGeometry, Float32BufferAttribute } from "three";
import { clone } from "three/addons/utils/SkeletonUtils.js";
import { models } from "./assets";
import { active, useStore } from "./store";
import {
  applyCamera,
  cameraTarget,
  defaultLighting,
  getAxisHint,
  lightingToCartesian,
  sampledShot,
  viewCone,
  type StageObject,
  type V3,
} from "./core";
import { viewportBridge } from "./viewportBridge";

function HintedTransformControls({
  object,
  mode,
  selected,
  onMouseDown,
  onObjectChange,
  onMouseUp,
  ...props
}: {
  object: any;
  mode: "translate" | "rotate" | "scale";
  selected: string;
  onMouseDown?: () => void;
  onObjectChange?: () => void;
  onMouseUp?: () => void;
  [key: string]: any;
}) {
  const tcRef = useRef<any>(null);

  useEffect(() => {
    const tc = tcRef.current;
    if (!tc) return;

    const onAxisChanged = (e: any) => {
      const axis = e?.value;
      if (!axis) {
        useStore.getState().setAxisHint(null);
      } else {
        const hint = getAxisHint(axis, mode, selected);
        useStore.getState().setAxisHint(hint);
      }
    };

    if (tc.axis) {
      const hint = getAxisHint(tc.axis, mode, selected);
      useStore.getState().setAxisHint(hint);
    }

    tc.addEventListener("axis-changed", onAxisChanged);
    return () => {
      tc.removeEventListener("axis-changed", onAxisChanged);
      useStore.getState().setAxisHint(null);
    };
  }, [mode, selected]);

  return (
    <TransformControls
      ref={tcRef}
      object={object}
      mode={mode}
      onMouseDown={onMouseDown}
      onObjectChange={onObjectChange}
      onMouseUp={onMouseUp}
      {...props}
    />
  );
}

const rad = (deg: number) => (deg * Math.PI) / 180;
/** 一段肢体：从关节向下的一根胶囊。 */
function Limb({ length, radius, color }: { length: number; radius: number; color: string }) {
  return (
    <mesh position={[0, -length / 2, 0]} castShadow>
      <capsuleGeometry args={[radius, Math.max(0.01, length - radius * 2), 6, 12]} />
      <meshStandardMaterial color={color} />
    </mesh>
  );
}
/** 占位人：髋为根，躯干、头、两臂、两腿按姿态的关节角度摆放。面朝 +Z。 */
function Figure({ color, pose }: { color: string; pose?: PoseId }) {
  const p = poseOf(pose);
  return (
    <group position={[0, hipHeight(p), 0]}>
      <group rotation={[rad(p.lean), rad(p.twist), 0]}>
        <mesh position={[0, RIG.torso / 2, 0]} castShadow>
          <capsuleGeometry args={[0.19, RIG.torso - 0.3, 8, 16]} />
          <meshStandardMaterial color={color} />
        </mesh>
        <group position={[0, RIG.torso + RIG.neck, 0]} rotation={[rad(p.headPitch), rad(p.headYaw), 0]}>
          <mesh position={[0, RIG.head * 0.6, 0]} castShadow>
            <sphereGeometry args={[RIG.head, 24, 20]} />
            <meshStandardMaterial color={color} roughness={0.6} />
          </mesh>
          {/* 鼻尖：看得出脸朝哪边 */}
          <mesh position={[0, RIG.head * 0.55, RIG.head * 0.95]}>
            <sphereGeometry args={[0.03, 10, 8]} />
            <meshStandardMaterial color={color} roughness={0.6} />
          </mesh>
        </group>
        {([[-1, p.right.arm], [1, p.left.arm]] as const).map(([side, a]) => (
          <group key={side} position={[side * RIG.shoulderX, RIG.torso - 0.06, 0]} rotation={[-rad(a.raise), 0, side * rad(a.out)]}>
            <Limb length={RIG.upperArm} radius={0.055} color={color} />
            <group position={[0, -RIG.upperArm, 0]} rotation={[-rad(a.elbow), 0, 0]}>
              <Limb length={RIG.forearm} radius={0.05} color={color} />
            </group>
          </group>
        ))}
      </group>
      {([[-1, p.right.leg], [1, p.left.leg]] as const).map(([side, l]) => (
        <group key={side} position={[side * RIG.hipX, 0, 0]} rotation={[-rad(l.hip), 0, 0]}>
          <Limb length={RIG.thigh} radius={0.08} color={color} />
          <group position={[0, -RIG.thigh, 0]} rotation={[rad(l.knee), 0, 0]}>
            <Limb length={RIG.shin} radius={0.065} color={color} />
          </group>
        </group>
      ))}
    </group>
  );
}

function createRampGeometry(size: [number, number, number]): BufferGeometry {
  const [w, h, l] = size;
  const hw = w / 2, hh = h / 2, hl = l / 2;
  const geom = new BufferGeometry();
  const positions = new Float32Array([
    // Bottom (y = -hh)
    -hw, -hh, -hl,   hw, -hh, -hl,   hw, -hh,  hl,
    -hw, -hh, -hl,   hw, -hh,  hl,  -hw, -hh,  hl,
    // Back (z = -hl)
    -hw, -hh, -hl,  -hw,  hh, -hl,   hw,  hh, -hl,
    -hw, -hh, -hl,   hw,  hh, -hl,   hw, -hh, -hl,
    // Slope
    -hw,  hh, -hl,  -hw, -hh,  hl,   hw, -hh,  hl,
    -hw,  hh, -hl,   hw, -hh,  hl,   hw,  hh, -hl,
    // Left
    -hw, -hh, -hl,  -hw, -hh,  hl,  -hw,  hh, -hl,
    // Right
    hw, -hh, -hl,   hw,  hh, -hl,   hw, -hh,  hl,
  ]);
  geom.setAttribute("position", new Float32BufferAttribute(positions, 3));
  geom.computeVertexNormals();
  return geom;
}

function Item({ o, edit }: { o: StageObject; edit: boolean }) {
  const ref = useRef<Group>(null!);
  const [drag, setDrag] = useState(false);
  const state = useStore();
  const model = useMemo(() => {
    const source = o.assetId ? models.get(o.assetId) : null;
    return source ? clone(source) : null;
  }, [o.assetId]);
  const rampGeometry = useMemo(() => {
    return o.shape === "ramp" ? createRampGeometry(o.size) : null;
  }, [o.shape, o.size[0], o.size[1], o.size[2]]);
  const selected = state.selected === o.id && edit;
  const content = (
    <group
      ref={ref}
      userData={{stageObjectId:o.id}}
      position={o.position}
      rotation={o.rotation}
      scale={o.scale}
      visible={o.visible}
      onPointerDown={(e) => {
        if (edit) {
          e.stopPropagation();
          if (state.groundPlacement) {
            const shot = active(state.project);
            const activeObj = shot.objects.find((obj) => obj.id === state.selected);
            if (activeObj && !activeObj.locked && activeObj.id !== o.id) {
              const newY = groundedPositionY(activeObj, e.point.y);
              state.object(activeObj.id, { position: [e.point.x, newY, e.point.z] });
              state.set({
                groundPlacement: false,
                notice: `已将 ${activeObj.name} 放置在 ${o.name} 表面 (高度 ${e.point.y.toFixed(2)}m)`,
              });
              return;
            }
          }
          state.set({ selected: o.id });
        }
      }}
    >
      {model ? (
        <primitive object={model} dispose={null} />
      ) : o.kind === "prop" ? (
        <mesh position={o.center} castShadow>
          {o.shape === "cylinder" ? (
            <cylinderGeometry args={[o.size[0] / 2, o.size[2] / 2, o.size[1], 32]} />
          ) : o.shape === "sphere" ? (
            <sphereGeometry args={[o.size[0] / 2, 32, 16]} />
          ) : o.shape === "ramp" && rampGeometry ? (
            <primitive object={rampGeometry} attach="geometry" />
          ) : (
            <boxGeometry args={o.size} />
          )}
          <meshStandardMaterial color={o.color} />
        </mesh>
      ) : (
        <Figure color={o.color} pose={o.pose} />
      )}
      {selected && !drag && (
        <mesh position={o.center}>
          <boxGeometry args={o.size.map((n) => n + 0.06) as V3} />
          <meshBasicMaterial
            color="#d8b878"
            wireframe
            transparent
            opacity={0.4}
          />
        </mesh>
      )}
    </group>
  );
  return (
    <>
      {content}
      {selected && !o.locked && o.visible && (
        <HintedTransformControls
          object={ref}
          selected={o.id}
          onObjectChange={() => {
            if (ref.current) {
              const g = ref.current;
              const scale =
                state.mode === "scale" ? Math.max(0.01, g.scale.x) : o.scale;
              if (state.mode === "scale") g.scale.setScalar(scale);
              state.object(o.id, {
                position: g.position.toArray() as V3,
                rotation: [g.rotation.x, g.rotation.y, g.rotation.z],
                scale,
              });
            }
          }}
          mode={state.mode}
          showX
          showY={state.mode !== "scale"}
          showZ={state.mode !== "scale"}
          onMouseDown={() => {
            setDrag(true);
            state.set({ playing: false });
          }}
          onMouseUp={() => {
            if (ref.current) {
              const g = ref.current;
              const scale =
                state.mode === "scale" ? Math.max(0.01, g.scale.x) : o.scale;
              state.object(o.id, {
                position: g.position.toArray() as V3,
                rotation: [g.rotation.x, g.rotation.y, g.rotation.z],
                scale,
              });
            }
            setDrag(false);
            state.commitHistory();
          }}
        />
      )}
    </>
  );
}
function CameraRig({ monitor = false }: { monitor?: boolean }) {
  const { camera, gl, scene } = useThree();
  const sync = () => {
    const state = useStore.getState(),
      shot = active(state.project);
    applyCamera(camera as PerspectiveCamera, shot.camera, shot.objects);
    gl.domElement.dataset.renderedCamera = JSON.stringify({
      time: state.time,
      position: camera.position.toArray(),
      target: cameraTarget(shot.camera, shot.objects),
      focalLength: shot.camera.focalLength,
      fov: (camera as PerspectiveCamera).fov,
      aspect: (camera as PerspectiveCamera).aspect,
      objects: shot.objects.map((o) => ({
        id: o.id,
        position: o.position,
        visible: o.visible,
      })),
    });
  };
  useFrame(sync);
  useEffect(() => {
    if (!monitor) return;
    const applyObjects = (shot: ReturnType<typeof active>) => {
      const found = new Set<string>();
      scene.traverse(node => {
        const id = node.userData.stageObjectId;
        const o = shot.objects.find(o => o.id === id);
        if (o) {
          node.position.set(...o.position);
          node.rotation.set(...o.rotation);
          node.scale.setScalar(o.scale);
          node.visible = o.visible;
          node.updateMatrix();
          found.add(id);
        }
      });
      if (shot.objects.some(o => !found.has(o.id)))
        throw Error("场景对象尚未全部加载，无法生成一致截图");
      scene.updateMatrixWorld(true);
      applyCamera(camera as PerspectiveCamera, shot.camera, shot.objects);
    };
    viewportBridge.getCanvas = () => gl.domElement;
    viewportBridge.prepareExport = (width: number, height: number) => {
      const previous = gl.getSize(new Vector2()),
        ratio = gl.getPixelRatio();
      gl.setPixelRatio(1);
      gl.setSize(width, height, false);
      return () => {
        gl.setPixelRatio(ratio);
        gl.setSize(previous.x, previous.y, false);
        gl.render(scene, camera);
      };
    };
    viewportBridge.renderFrame = (shot, time) => {
      const current = active(useStore.getState().project);
      const sampled = sampledShot(shot, time);
      applyObjects(sampled);
      gl.render(scene, camera);
      applyObjects(current);
    };
    viewportBridge.capture = (snapshot) => {
      const current=active(useStore.getState().project);
      applyObjects(snapshot??current);
      const previous = gl.getSize(new Vector2()),
        ratio = gl.getPixelRatio();
      try {
        gl.setPixelRatio(1);
        gl.setSize(1920, 1080, false);
        gl.render(scene, camera);
        return gl.domElement.toDataURL("image/png");
      } finally {
        applyObjects(current);
        gl.setPixelRatio(ratio);
        gl.setSize(previous.x, previous.y, false);
        gl.render(scene, camera);
      }
    };
    return () => {
      viewportBridge.capture = null;
      viewportBridge.renderFrame = null;
      viewportBridge.getCanvas = null;
    };
  }, [monitor, gl, scene, camera]);
  return null;
}
function CameraVisual() {
  const state = useStore(),
    shot = active(state.project),
    c = shot.camera;
  const rig = useRef<Group>(null!),
    aim = useRef<Group>(null!);
  const cam = useMemo(
    () => applyCamera(new PerspectiveCamera(), c, shot.objects),
    [c, shot.objects],
  );
  const target = cameraTarget(c, shot.objects),
    cone = viewCone(c, shot.objects);
  const { gl } = useThree();
  useLayoutEffect(() => {
    if (rig.current) {
      rig.current.position.copy(cam.position);
      rig.current.quaternion.copy(cam.quaternion);
    }
    if (aim.current) aim.current.position.set(...target);
    gl.domElement.dataset.frustum = JSON.stringify(cone);
  }, [cam, target, cone, gl]);
  const positionMode =
    state.selected === "camera-target" || state.mode === "scale"
      ? "translate"
      : state.mode;
  const commit = () => {
    if (state.selected === "camera-target") {
      state.camera({
        lookAt: null,
        target: aim.current.position.toArray() as V3,
      });
      return;
    }
    if (positionMode === "rotate") {
      const distance = Math.max(
        0.1,
        new Vector3(...target).distanceTo(new Vector3(...c.position)),
      );
      const t = new Vector3(0, 0, -1)
        .applyQuaternion(rig.current.quaternion)
        .multiplyScalar(distance)
        .add(rig.current.position);
      const neutral = applyCamera(
        new PerspectiveCamera(),
        { ...c, target: t.toArray() as V3, lookAt: null, roll: 0 },
        shot.objects,
      );
      const delta = neutral.quaternion
        .clone()
        .invert()
        .multiply(rig.current.quaternion);
      state.camera({
        lookAt: null,
        target: t.toArray() as V3,
        roll: new Euler().setFromQuaternion(delta).z,
      });
    } else state.camera({ position: rig.current.position.toArray() as V3 });
  };
  const keys = shot.keys
    .filter((k) => k.target === "camera")
    .sort((a, b) => a.time - b.time);
  return (
    <>
      <group
        ref={rig}
        onPointerDown={(e) => {
          e.stopPropagation();
          state.set({ selected: "camera" });
        }}
      >
        <mesh>
          <boxGeometry args={[0.4, 0.28, 0.5]} />
          <meshStandardMaterial color="#d8b878" />
        </mesh>
        <mesh position={[0, 0, -0.34]} rotation={[Math.PI / 2, 0, 0]}>
          <cylinderGeometry args={[0.15, 0.12, 0.25, 12]} />
          <meshStandardMaterial color="#dfc58f" />
        </mesh>
        <mesh position={[0, 0.19, 0.08]}>
          <boxGeometry args={[0.2, 0.12, 0.2]} />
          <meshStandardMaterial color="#846e4b" />
        </mesh>
      </group>
      <group
        ref={aim}
        onPointerDown={(e) => {
          e.stopPropagation();
          state.set({ selected: "camera-target", mode: "translate" });
        }}
      >
        <mesh>
          <sphereGeometry args={[0.07, 16, 12]} />
          <meshBasicMaterial color="#74c9d0" depthTest={false} />
        </mesh>
      </group>
      {state.showFrustum && (
        <>
          {cone.corners.map((corner, i) => (
            <Line
              key={i}
              points={[cone.origin, corner]}
              color="#d7b976"
              depthTest={false}
            dashed
              dashSize={0.14}
              gapSize={0.1}
              transparent
              opacity={0.7}
              raycast={() => null}
            />
          ))}
          <Line
            points={[...cone.corners, cone.corners[0]]}
            color="#e6c990"
            depthTest={false}
            dashed
            dashSize={0.14}
            gapSize={0.1}
            raycast={() => null}
          />
          <Line
            points={[c.position, target]}
            color="#79c3ca"
            depthTest={false}
            dashed
            dashSize={0.08}
            gapSize={0.13}
            transparent
            opacity={0.55}
            raycast={() => null}
          />
        </>
      )}
      {keys.length > 1 && (
        <Line
          points={keys.map((k) => k.position)}
          color="#77b5d1"
          dashed
          dashSize={0.12}
          gapSize={0.07}
          raycast={() => null}
        />
      )}
      {keys.map((k) => (
        <mesh
          key={k.id}
          position={k.position}
          onClick={(e) => {
            e.stopPropagation();
            state.set({ playing: false, selected: "camera" });
            state.seek(k.time);
          }}
        >
          <sphereGeometry args={[0.055, 12, 8]} />
          <meshBasicMaterial color="#77b5d1" />
        </mesh>
      ))}
      {state.selected.startsWith("camera") && (
        <HintedTransformControls
          object={state.selected === "camera-target" ? aim : rig}
          selected={state.selected}
          mode={positionMode}
          showZ
          onMouseDown={() => state.set({ playing: false })}
          onObjectChange={commit}
          onMouseUp={() => state.commitHistory()}
        />
      )}
    </>
  );
}
function EditorNavigation() {
  const controls = useRef<any>(null),
    { camera } = useThree();
  useEffect(() => {
    camera.position.set(8, 6, 9);
    camera.lookAt(0, 1, 0);
    (camera as PerspectiveCamera).fov = 45;
    (camera as PerspectiveCamera).updateProjectionMatrix();
    viewportBridge.useEditorAsCamera = () => {
      const state = useStore.getState(),
        c = active(state.project).camera;
      const cam = camera as PerspectiveCamera;
      const focal =
        c.sensorWidth / (16 / 9) / (2 * Math.tan((cam.fov * Math.PI) / 360));
      state.camera({
        position: camera.position.toArray() as V3,
        target: controls.current.target.toArray() as V3,
        lookAt: null,
        focalLength: Math.max(8, Math.min(300, focal)),
      });
      state.set({ notice: "已将自由视角设为主摄影机", selected: "camera" });
    };
    return () => {
      viewportBridge.useEditorAsCamera = null;
    };
  }, [camera]);
  return (
    <OrbitControls
      ref={controls}
      makeDefault
      target={[0, 1, 0]}
      minDistance={0.3}
      maxDistance={100}
      enableDamping={false}
    />
  );
}
function PilotControls() {
  const controls = useRef<any>(null),
    interacting = useRef(false);
  const { camera } = useThree();
  const state = useStore(),
    shot = active(state.project);
  useLayoutEffect(() => {
    if (!interacting.current && controls.current) {
      applyCamera(camera as PerspectiveCamera, shot.camera, shot.objects);
      controls.current.target.set(...cameraTarget(shot.camera, shot.objects));
      controls.current.update();
    }
  }, [shot.camera, shot.objects, camera]);
  return (
    <OrbitControls
      ref={controls}
      makeDefault
      enableDamping={false}
      minDistance={0.05}
      onStart={() => {
        interacting.current = true;
        state.set({ playing: false });
      }}
      onEnd={() => {
        interacting.current = false;
      }}
      onChange={() => {
        if (interacting.current && controls.current)
          state.camera({
            position: camera.position.toArray() as V3,
            target: controls.current.target.toArray() as V3,
            lookAt: null,
          });
      }}
    />
  );
}
function SceneBackground() {
  const state=useStore(), shot=active(state.project), board=boardFor(shot);
  const texture=shot.backgroundImageId?imageTextures.get(shot.backgroundImageId):null;
  const euler = useMemo(() => new Euler(board.pitch ?? 0, board.yaw, 0, 'YXZ'), [board.pitch, board.yaw]);
  return <>
    <color attach="background" args={["#242b33"]} />
    {texture&&<mesh position={board.position} rotation={euler}>
      <planeGeometry args={[board.width,board.height]} />
      <meshBasicMaterial map={texture} side={DoubleSide} toneMapped={false} />
    </mesh>}
  </>;
}
function World({ edit }: { edit: boolean }) {
  const state = useStore(),
    shot = active(state.project),
    stage = edit && state.editorView === "stage";
  const lighting = shot.lighting ?? defaultLighting();

  const keyPos = useMemo(
    () =>
      lightingToCartesian(
        lighting.keyAzimuth,
        lighting.keyElevation,
        lighting.keyDistance ?? 8,
        1,
      ),
    [lighting.keyAzimuth, lighting.keyElevation, lighting.keyDistance],
  );

  const fillPos = useMemo(
    () =>
      lightingToCartesian(
        lighting.fillAzimuth,
        lighting.fillElevation,
        7,
        1,
      ),
    [lighting.fillAzimuth, lighting.fillElevation],
  );

  const rimPos = useMemo(
    () =>
      lightingToCartesian(
        lighting.rimAzimuth,
        lighting.rimElevation,
        6,
        1.2,
      ),
    [lighting.rimAzimuth, lighting.rimElevation],
  );

  return (
    <>
      <SceneBackground />
      <ambientLight
        intensity={lighting.ambientIntensity}
        color={lighting.ambientColor}
      />
      <hemisphereLight
        args={[
          lighting.keyColor,
          "#22201d",
          lighting.groundBounceIntensity ?? 0.35,
        ]}
      />
      <directionalLight
        position={keyPos}
        intensity={lighting.keyIntensity}
        color={lighting.keyColor}
        castShadow
        shadow-mapSize={[2048, 2048]}
        shadow-camera-near={0.5}
        shadow-camera-far={25}
        shadow-camera-left={-8}
        shadow-camera-right={8}
        shadow-camera-top={8}
        shadow-camera-bottom={-8}
        shadow-bias={-0.0005}
      />
      {lighting.fillEnabled && (
        <directionalLight
          position={fillPos}
          intensity={lighting.fillIntensity}
          color={lighting.fillColor}
        />
      )}
      {lighting.rimEnabled && (
        <directionalLight
          position={rimPos}
          intensity={lighting.rimIntensity}
          color={lighting.rimColor}
        />
      )}
      <mesh
        rotation={[-Math.PI / 2, 0, 0]}
        position={[0, -0.018, 0]}
        receiveShadow
        onClick={e=>{
          if(!stage||!state.groundPlacement)return;
          e.stopPropagation();
          const o=shot.objects.find(o=>o.id===state.selected);
          if(!o||o.locked){state.set({notice:"先选择一个未锁定的角色或道具"});return;}
          const newY = groundedPositionY(o, e.point.y);
          state.object(o.id,{position:[e.point.x, newY, e.point.z]});
          state.set({groundPlacement:false,notice:`已将 ${o.name} 贴地放置在地面 Y=${e.point.y.toFixed(2)}m`});
        }}
      >
        <planeGeometry args={[200, 200]} />
        <meshStandardMaterial color="#303940" roughness={1} />
      </mesh>
      {stage && (
        <Grid
          args={[30, 30]}
          cellSize={1}
          sectionSize={5}
          cellColor="#454d57"
          sectionColor="#69737d"
          fadeDistance={35}
          infiniteGrid
        />
      )}
      {shot.objects.map((o) => (
        <Item key={o.id} o={o} edit={stage} />
      ))}
      {stage ? (
        <>
          <CameraVisual />
          <EditorNavigation />
          <GizmoHelper alignment="bottom-right" margin={[65, 65]}>
            <GizmoViewport
              axisColors={["#b97b78", "#8ba58c", "#7e9bbd"]}
              labelColor="white"
            />
          </GizmoHelper>
        </>
      ) : (
        <>
          <CameraRig monitor={!edit} />
          {edit && state.pilot && <PilotControls />}
        </>
      )}
    </>
  );
}
export default function Stage({ edit = false }: { edit?: boolean }) {
  const mode = useStore((s) => s.editorView),
    axisHint = useStore((s) => s.axisHint),
    container = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [pointerPos, setPointerPos] = useState<{ x: number; y: number } | null>(null);

  useEffect(() => {
    if (!container.current) return;
    const observer = new ResizeObserver(([entry]) =>
      setSize({
        width: entry.contentRect.width,
        height: entry.contentRect.height,
      }),
    );
    observer.observe(container.current);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!edit) return;
    const onMove = (e: PointerEvent) => {
      if (!container.current) return;
      const rect = container.current.getBoundingClientRect();
      if (
        e.clientX >= rect.left &&
        e.clientX <= rect.right &&
        e.clientY >= rect.top &&
        e.clientY <= rect.bottom
      ) {
        setPointerPos({
          x: e.clientX - rect.left,
          y: e.clientY - rect.top,
        });
      }
    };
    window.addEventListener("pointermove", onMove);
    return () => window.removeEventListener("pointermove", onMove);
  }, [edit]);

  const handlePointerLeave = () => {
    setPointerPos(null);
    useStore.getState().setAxisHint(null);
  };

  const width =
    edit && mode === "camera"
      ? Math.min(size.width, (size.height * 16) / 9)
      : size.width;
  const height = edit && mode === "camera" ? (width * 9) / 16 : size.height;

  let tooltipX = 0;
  let tooltipY = 0;
  if (pointerPos) {
    const tooltipWidth = 240;
    const tooltipHeight = 72;
    tooltipX = pointerPos.x + 18;
    tooltipY = pointerPos.y + 18;
    if (size.width > 0 && tooltipX + tooltipWidth > size.width - 10) {
      tooltipX = pointerPos.x - tooltipWidth - 12;
    }
    if (size.height > 0 && tooltipY + tooltipHeight > size.height - 10) {
      tooltipY = pointerPos.y - tooltipHeight - 12;
    }
    tooltipX = Math.max(10, tooltipX);
    tooltipY = Math.max(10, tooltipY);
  }

  return (
    <div
      ref={container}
      className="stage-surface"
      data-view={edit ? "editor" : "monitor"}
      onPointerLeave={handlePointerLeave}
    >
      <div
        style={{
          width: width || "100%",
          height: height || "100%",
          position: "absolute",
          left: "50%",
          top: "50%",
          transform: "translate(-50%,-50%)",
        }}
      >
        <Canvas
          shadows
          dpr={[1, 1.5]}
          camera={{ position: [8, 6, 9], fov: 45, near: 0.03, far: 1000 }}
          gl={{ preserveDrawingBuffer: !edit, antialias: true }}
          onPointerMissed={() =>
            edit && useStore.getState().set({ selected: "camera" })
          }
        >
          <World edit={edit} />
        </Canvas>
      </div>

      {edit && axisHint && pointerPos && (
        <div
          className="axis-tooltip-overlay"
          style={{
            left: tooltipX,
            top: tooltipY,
          }}
        >
          <div className="axis-tooltip-header">
            <span
              className="axis-tooltip-badge"
              style={{
                color: axisHint.color,
                borderColor: axisHint.color,
                backgroundColor: `${axisHint.color}26`,
              }}
            >
              {axisHint.axis} 轴
            </span>
            <span className="axis-tooltip-title">{axisHint.title}</span>
          </div>
          <div className="axis-tooltip-desc">{axisHint.desc}</div>
        </div>
      )}
    </div>
  );
}
