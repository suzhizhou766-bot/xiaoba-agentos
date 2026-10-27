import * as THREE from "three";
import { GLTFExporter } from "three/addons/exporters/GLTFExporter.js";
import * as fs from "fs";
import * as path from "path";

// Polyfill FileReader for Node.js
class NodeFileReader {
  onloadend: any;
  result: any;
  async readAsArrayBuffer(blob: any) {
    this.result = await blob.arrayBuffer();
    if (this.onloadend) this.onloadend();
  }
}
(globalThis as any).FileReader = NodeFileReader;

function exportGLB(group: THREE.Group): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const exporter = new GLTFExporter();
    exporter.parse(
      group,
      (gltf) => {
        resolve(Buffer.from(gltf as ArrayBuffer));
      },
      (err) => reject(err),
      { binary: true }
    );
  });
}

export interface DirectorPropDef {
  id: string;
  name: string;
  category: "filming_gear" | "lighting" | "blocking" | "narrative" | "architecture";
  categoryZh: string;
  icon: string;
  description: string;
  build: () => THREE.Group;
}

const definitions: DirectorPropDef[] = [
  // 1. 导演折叠椅 (Director's Chair)
  {
    id: "director_chair",
    name: "经典导演椅",
    category: "blocking",
    categoryZh: "导演调度",
    icon: "🎬",
    description: "经典高脚木质帆布折叠导演椅，带搁脚踏板、双侧扶手与黑色帆布靠背，片场标志性装备",
    build: () => {
      const g = new THREE.Group();
      g.name = "DirectorChair";
      const woodMat = new THREE.MeshStandardMaterial({ color: 0x6e421f, roughness: 0.5 });
      const canvasMat = new THREE.MeshStandardMaterial({ color: 0x11161d, roughness: 0.85 });
      const metalMat = new THREE.MeshStandardMaterial({ color: 0x718096, roughness: 0.3, metalness: 0.8 });

      // Criss-cross X legs (Front & Back pairs)
      const legGeo = new THREE.BoxGeometry(0.035, 0.95, 0.025);
      [-0.22, 0.22].forEach((z) => {
        const l1 = new THREE.Mesh(legGeo, woodMat);
        l1.position.set(0, 0.45, z);
        l1.rotation.z = 0.28;
        g.add(l1);

        const l2 = new THREE.Mesh(legGeo, woodMat);
        l2.position.set(0, 0.45, z);
        l2.rotation.z = -0.28;
        g.add(l2);

        // Center pivot screw
        const pivot = new THREE.Mesh(new THREE.CylinderGeometry(0.015, 0.015, 0.04, 8), metalMat);
        pivot.rotation.x = Math.PI / 2;
        pivot.position.set(0, 0.45, z);
        g.add(pivot);
      });

      // Canvas Seat
      const seat = new THREE.Mesh(new THREE.BoxGeometry(0.48, 0.03, 0.42), canvasMat);
      seat.position.set(0, 0.74, 0);
      seat.castShadow = true;
      g.add(seat);

      // Armrests & Vertical Supports
      [-0.24, 0.24].forEach((x) => {
        const post = new THREE.Mesh(new THREE.CylinderGeometry(0.018, 0.018, 0.45, 8), woodMat);
        post.position.set(x, 0.92, -0.2);
        g.add(post);

        const arm = new THREE.Mesh(new THREE.BoxGeometry(0.05, 0.025, 0.44), woodMat);
        arm.position.set(x, 0.94, 0);
        g.add(arm);

        const frontPost = new THREE.Mesh(new THREE.CylinderGeometry(0.015, 0.015, 0.2, 8), woodMat);
        frontPost.position.set(x, 0.84, 0.18);
        g.add(frontPost);
      });

      // Canvas Backrest
      const back = new THREE.Mesh(new THREE.BoxGeometry(0.46, 0.22, 0.02), canvasMat);
      back.position.set(0, 1.05, -0.2);
      back.castShadow = true;
      g.add(back);

      // Footrest bar
      const footrest = new THREE.Mesh(new THREE.BoxGeometry(0.46, 0.02, 0.06), woodMat);
      footrest.position.set(0, 0.32, 0.15);
      g.add(footrest);

      return g;
    },
  },

  // 2. 苹果箱组 (Apple Boxes)
  {
    id: "apple_box_full",
    name: "标准苹果箱 (全号)",
    category: "blocking",
    categoryZh: "导演调度",
    icon: "🧰",
    description: "影视工业标准 Full Apple Box (20\"×12\"×8\")，垫高演员身高差、抬高机位或坐靠歇脚必备",
    build: () => {
      const g = new THREE.Group();
      g.name = "AppleBox";
      const plyMat = new THREE.MeshStandardMaterial({ color: 0xbfa17a, roughness: 0.6 });
      const holeMat = new THREE.MeshStandardMaterial({ color: 0x3d3023, roughness: 0.8 });

      // Core box (0.50m x 0.20m x 0.30m)
      const box = new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.2, 0.3), plyMat);
      box.position.set(0, 0.1, 0);
      box.castShadow = true;
      g.add(box);

      // Oval Handle insets on both short ends
      [-0.251, 0.251].forEach((x) => {
        const handle = new THREE.Mesh(new THREE.BoxGeometry(0.005, 0.045, 0.12), holeMat);
        handle.position.set(x, 0.1, 0);
        g.add(handle);
      });

      return g;
    },
  },

  // 3. 走位 T 型地标 (Actor T-Marker)
  {
    id: "actor_t_marker",
    name: "演员走位 T 型地标",
    category: "blocking",
    categoryZh: "导演调度",
    icon: "🎯",
    description: "片场铸铅 T 型演员站位标记（亮黄色高对比度），用于标记起幅、中途停顿点与落幅走位",
    build: () => {
      const g = new THREE.Group();
      g.name = "TMarker";
      const yellowMat = new THREE.MeshStandardMaterial({ color: 0xf39c12, roughness: 0.4, metalness: 0.3 });

      // Top horizontal bar of T
      const topBar = new THREE.Mesh(new THREE.BoxGeometry(0.3, 0.015, 0.07), yellowMat);
      topBar.position.set(0, 0.008, -0.065);
      g.add(topBar);

      // Vertical stem of T
      const stem = new THREE.Mesh(new THREE.BoxGeometry(0.07, 0.015, 0.2), yellowMat);
      stem.position.set(0, 0.008, 0.05);
      g.add(stem);

      return g;
    },
  },

  // 4. 影视场记板 (Movie Slate / Clapperboard)
  {
    id: "clapperboard",
    name: "专业影视场记板",
    category: "blocking",
    categoryZh: "导演调度",
    icon: "📋",
    description: "开拍打板工具，带黑白斑马警示斜纹铰链与场次、镜次、导演记录板面",
    build: () => {
      const g = new THREE.Group();
      g.name = "Clapperboard";
      const slateMat = new THREE.MeshStandardMaterial({ color: 0x11161d, roughness: 0.3 });
      const whiteMat = new THREE.MeshStandardMaterial({ color: 0xf1f5f9, roughness: 0.4 });
      const metalMat = new THREE.MeshStandardMaterial({ color: 0x718096, roughness: 0.2, metalness: 0.9 });

      // Main board (0.30m x 0.22m x 0.015m)
      const slate = new THREE.Mesh(new THREE.BoxGeometry(0.3, 0.22, 0.015), slateMat);
      slate.position.set(0, 0.11, 0);
      slate.castShadow = true;
      g.add(slate);

      // White label lines proxy
      [-0.04, 0.02, 0.08].forEach((y) => {
        const line = new THREE.Mesh(new THREE.BoxGeometry(0.26, 0.005, 0.017), whiteMat);
        line.position.set(0, 0.11 + y, 0);
        g.add(line);
      });

      // Lower fixed clapper stick
      const fixedStick = new THREE.Mesh(new THREE.BoxGeometry(0.3, 0.035, 0.02), slateMat);
      fixedStick.position.set(0, 0.235, 0);
      g.add(fixedStick);

      // Hinged upper clapper stick (tilted open 22 degrees)
      const hingedGroup = new THREE.Group();
      hingedGroup.position.set(-0.15, 0.25, 0);
      hingedGroup.rotation.z = 0.38; // ~22 deg

      const upperStick = new THREE.Mesh(new THREE.BoxGeometry(0.3, 0.035, 0.02), slateMat);
      upperStick.position.set(0.15, 0.02, 0);
      hingedGroup.add(upperStick);

      // Black & White diagonal stripes on sticks
      [-0.1, -0.02, 0.06, 0.14].forEach((offset) => {
        const stripe = new THREE.Mesh(new THREE.BoxGeometry(0.035, 0.036, 0.022), whiteMat);
        stripe.position.set(0.15 + offset, 0.02, 0);
        hingedGroup.add(stripe);
      });

      g.add(hingedGroup);

      // Hinge screw
      const screw = new THREE.Mesh(new THREE.CylinderGeometry(0.01, 0.01, 0.03, 8), metalMat);
      screw.rotation.x = Math.PI / 2;
      screw.position.set(-0.13, 0.25, 0);
      g.add(screw);

      return g;
    },
  },

  // 5. 摄影轨道推车 (Camera Dolly & Track)
  {
    id: "camera_dolly_track",
    name: "摄影轨道与移动推车",
    category: "filming_gear",
    categoryZh: "摄影器材",
    icon: "🛤️",
    description: "专业电影双轨滑轨（2.5米）与四轮低重心移动平车，直观标示镜头推拉与横移滑行路径",
    build: () => {
      const g = new THREE.Group();
      g.name = "CameraDolly";
      const steelMat = new THREE.MeshStandardMaterial({ color: 0x718096, roughness: 0.25, metalness: 0.85 });
      const woodTieMat = new THREE.MeshStandardMaterial({ color: 0x3e2723, roughness: 0.7 });
      const platformMat = new THREE.MeshStandardMaterial({ color: 0x1a202c, roughness: 0.4, metalness: 0.6 });
      const wheelMat = new THREE.MeshStandardMaterial({ color: 0x2b6cb0, roughness: 0.3, metalness: 0.4 });

      // Two 2.6m Parallel Tubular Rails
      const railGeo = new THREE.CylinderGeometry(0.025, 0.025, 2.6, 12);
      railGeo.rotateX(Math.PI / 2);
      [-0.31, 0.31].forEach((x) => {
        const rail = new THREE.Mesh(railGeo, steelMat);
        rail.position.set(x, 0.06, 0);
        g.add(rail);
      });

      // 5 Cross ties / sleepers
      [-1.0, -0.5, 0, 0.5, 1.0].forEach((z) => {
        const tie = new THREE.Mesh(new THREE.BoxGeometry(0.74, 0.035, 0.08), woodTieMat);
        tie.position.set(0, 0.018, z);
        g.add(tie);
      });

      // Dolly Flatbed Platform
      const platform = new THREE.Mesh(new THREE.BoxGeometry(0.72, 0.04, 0.72), platformMat);
      platform.position.set(0, 0.13, 0);
      platform.castShadow = true;
      g.add(platform);

      // Central Mitchell camera mount / pedestal collar
      const mount = new THREE.Mesh(new THREE.CylinderGeometry(0.12, 0.14, 0.06, 16), steelMat);
      mount.position.set(0, 0.18, 0);
      g.add(mount);

      // 4 Bogie wheel sets riding on rails
      const wheelGeo = new THREE.CylinderGeometry(0.04, 0.04, 0.04, 12);
      wheelGeo.rotateZ(Math.PI / 2);
      [[-0.31, -0.28], [-0.31, 0.28], [0.31, -0.28], [0.31, 0.28]].forEach(([x, z]) => {
        const wheel = new THREE.Mesh(wheelGeo, wheelMat);
        wheel.position.set(x, 0.08, z);
        g.add(wheel);
      });

      // Push bar handle
      const handle = new THREE.Mesh(new THREE.CylinderGeometry(0.015, 0.015, 0.6, 8), steelMat);
      handle.position.set(0, 0.65, 0.35);
      g.add(handle);

      return g;
    },
  },

  // 6. 重型电影三脚架 (Cinema Tripod)
  {
    id: "cinema_tripod",
    name: "影视重型三脚架",
    category: "filming_gear",
    categoryZh: "摄影器材",
    icon: "🎥",
    description: "碳纤维双级重型三脚架，带地面延伸撑脚与液压云台，用于建立副机位与视线参考",
    build: () => {
      const g = new THREE.Group();
      g.name = "CinemaTripod";
      const carbonMat = new THREE.MeshStandardMaterial({ color: 0x1a202c, roughness: 0.3, metalness: 0.5 });
      const metalMat = new THREE.MeshStandardMaterial({ color: 0xa0aec0, roughness: 0.2, metalness: 0.85 });

      // Spreader (Ground triangle base)
      [0, (Math.PI * 2) / 3, (Math.PI * 4) / 3].forEach((angle) => {
        const spreader = new THREE.Mesh(new THREE.BoxGeometry(0.02, 0.015, 0.45), carbonMat);
        spreader.rotation.y = angle;
        spreader.position.set(Math.sin(angle) * 0.22, 0.01, Math.cos(angle) * 0.22);
        g.add(spreader);
      });

      // 3 Twin-tube legs converging up to 1.3m
      const legGeo = new THREE.CylinderGeometry(0.016, 0.016, 1.45, 8);
      [0, (Math.PI * 2) / 3, (Math.PI * 4) / 3].forEach((angle) => {
        const legGroup = new THREE.Group();
        legGroup.rotation.y = angle;

        const l1 = new THREE.Mesh(legGeo, carbonMat);
        l1.position.set(-0.025, 0.72, 0.22);
        l1.rotation.x = -0.32;
        legGroup.add(l1);

        const l2 = new THREE.Mesh(legGeo, carbonMat);
        l2.position.set(0.025, 0.72, 0.22);
        l2.rotation.x = -0.32;
        legGroup.add(l2);

        g.add(legGroup);
      });

      // Bowl and Fluid Head
      const head = new THREE.Mesh(new THREE.CylinderGeometry(0.1, 0.08, 0.14, 16), metalMat);
      head.position.set(0, 1.38, 0);
      g.add(head);

      // Pan handle
      const panHandle = new THREE.Mesh(new THREE.CylinderGeometry(0.01, 0.01, 0.38, 8), metalMat);
      panHandle.position.set(-0.08, 1.34, -0.18);
      panHandle.rotation.x = 0.6;
      g.add(panHandle);

      // Quick release plate top
      const plate = new THREE.Mesh(new THREE.BoxGeometry(0.14, 0.025, 0.2), carbonMat);
      plate.position.set(0, 1.46, 0);
      g.add(plate);

      return g;
    },
  },

  // 7. 魔术腿与遮光旗板 (C-Stand with Flag)
  {
    id: "c_stand_flag",
    name: "魔术腿与遮光黑旗板",
    category: "lighting",
    categoryZh: "灯光控光",
    icon: "🚏",
    description: "影视片场最核心的控光工具（C-Stand + 24\"×36\" Solid Flag），用于演员切光、减光与防镜头眩光",
    build: () => {
      const g = new THREE.Group();
      g.name = "CStand";
      const chromeMat = new THREE.MeshStandardMaterial({ color: 0xdce0e6, roughness: 0.15, metalness: 0.9 });
      const flagMat = new THREE.MeshStandardMaterial({ color: 0x0a0c10, roughness: 0.95 });

      // Turtle Base - 3 staggered curved legs
      const legAngles = [0, 2.1, 4.2];
      const legHeights = [0.03, 0.06, 0.09];
      legAngles.forEach((a, i) => {
        const leg = new THREE.Mesh(new THREE.CylinderGeometry(0.014, 0.014, 0.45, 8), chromeMat);
        leg.rotation.z = Math.PI / 2;
        leg.rotation.y = a;
        leg.position.set(Math.sin(a) * 0.22, legHeights[i], Math.cos(a) * 0.22);
        g.add(leg);
      });

      // Main Vertical Riser Column (up to 1.8m)
      const column = new THREE.Mesh(new THREE.CylinderGeometry(0.018, 0.018, 1.85, 12), chromeMat);
      column.position.set(0, 0.93, 0);
      g.add(column);

      // Grip Head at top
      const gripHead = new THREE.Mesh(new THREE.CylinderGeometry(0.035, 0.035, 0.07, 12), chromeMat);
      gripHead.rotation.x = Math.PI / 2;
      gripHead.position.set(0, 1.82, 0);
      g.add(gripHead);

      // 40\" Extension Grip Arm
      const arm = new THREE.Mesh(new THREE.CylinderGeometry(0.012, 0.012, 1.05, 8), chromeMat);
      arm.position.set(0.42, 1.95, 0);
      arm.rotation.z = -Math.PI / 2.8;
      g.add(arm);

      // 24\" x 36\" Black Solid Cutter Flag
      const flag = new THREE.Mesh(new THREE.BoxGeometry(0.6, 0.9, 0.015), flagMat);
      flag.position.set(0.72, 1.98, 0);
      flag.rotation.y = 0.25;
      flag.rotation.z = -0.1;
      flag.castShadow = true;
      g.add(flag);

      return g;
    },
  },

  // 8. 影室柔光箱 (Studio Softbox)
  {
    id: "studio_softbox",
    name: "影室柔光箱 (主光灯)",
    category: "lighting",
    categoryZh: "灯光控光",
    icon: "💡",
    description: "影视标准主光柔光箱，0.8米方形扩散柔光屏与蜂巢格栅，模拟窗光、室内大漫射柔和人物面光",
    build: () => {
      const g = new THREE.Group();
      g.name = "Softbox";
      const standMat = new THREE.MeshStandardMaterial({ color: 0x2d3748, roughness: 0.4, metalness: 0.7 });
      const blackFabric = new THREE.MeshStandardMaterial({ color: 0x11161d, roughness: 0.9 });
      const diffuserMat = new THREE.MeshStandardMaterial({
        color: 0xfffbf0,
        roughness: 0.2,
        emissive: 0xfff4d6,
        emissiveIntensity: 0.7,
      });

      // Tripod stand legs
      [0, (Math.PI * 2) / 3, (Math.PI * 4) / 3].forEach((angle) => {
        const leg = new THREE.Mesh(new THREE.CylinderGeometry(0.012, 0.012, 0.55, 8), standMat);
        leg.rotation.z = 0.55;
        leg.rotation.y = angle;
        leg.position.set(Math.sin(angle) * 0.25, 0.22, Math.cos(angle) * 0.25);
        g.add(leg);
      });

      // Vertical Stand Column up to 1.9m
      const col = new THREE.Mesh(new THREE.CylinderGeometry(0.018, 0.02, 1.85, 10), standMat);
      col.position.set(0, 0.93, 0);
      g.add(col);

      // Light head mount
      const mountGroup = new THREE.Group();
      mountGroup.position.set(0, 1.88, 0);
      mountGroup.rotation.x = 0.28; // Tilted downwards 16 degrees

      // Pyramid Softbox Housing (Front opening: 0.8m x 0.8m, Depth: 0.5m)
      const housing = new THREE.Mesh(new THREE.ConeGeometry(0.56, 0.52, 4, 1, true), blackFabric);
      housing.position.set(0, 0, -0.26);
      housing.rotation.y = Math.PI / 4;
      housing.rotation.x = Math.PI / 2;
      mountGroup.add(housing);

      // Front Diffuser Screen
      const screen = new THREE.Mesh(new THREE.PlaneGeometry(0.78, 0.78), diffuserMat);
      screen.position.set(0, 0, 0);
      mountGroup.add(screen);

      g.add(mountGroup);
      return g;
    },
  },

  // 9. 四叶遮扉聚光灯 (Fresnel Spotlight with Barn Doors)
  {
    id: "fresnel_spotlight",
    name: "四叶遮扉聚光灯",
    category: "lighting",
    categoryZh: "灯光控光",
    icon: "🔦",
    description: "经典菲涅尔聚光灯（带四扇金属挡光叶门），用于轮廓光、发丝光（Rim Light）与精准局部投光",
    build: () => {
      const g = new THREE.Group();
      g.name = "Spotlight";
      const metalMat = new THREE.MeshStandardMaterial({ color: 0x222a36, roughness: 0.35, metalness: 0.7 });
      const silverMat = new THREE.MeshStandardMaterial({ color: 0x94a3b8, roughness: 0.2, metalness: 0.9 });
      const lensMat = new THREE.MeshStandardMaterial({
        color: 0xffffff,
        roughness: 0.1,
        emissive: 0xffea9f,
        emissiveIntensity: 0.9,
      });

      // Stand
      const stand = new THREE.Mesh(new THREE.CylinderGeometry(0.016, 0.016, 1.9, 8), metalMat);
      stand.position.set(0, 0.95, 0);
      g.add(stand);

      // Yoke (U-shaped bracket)
      const yoke = new THREE.Mesh(new THREE.BoxGeometry(0.32, 0.22, 0.03), silverMat);
      yoke.position.set(0, 1.95, 0);
      g.add(yoke);

      // Lantern Lamp Head (Cylinder)
      const headGroup = new THREE.Group();
      headGroup.position.set(0, 1.95, 0);
      headGroup.rotation.x = 0.32; // Downward tilt

      const body = new THREE.Mesh(new THREE.CylinderGeometry(0.12, 0.12, 0.32, 16), metalMat);
      body.rotation.x = Math.PI / 2;
      headGroup.add(body);

      // Front Lens
      const lens = new THREE.Mesh(new THREE.CircleGeometry(0.11, 16), lensMat);
      lens.position.set(0, 0, 0.161);
      headGroup.add(lens);

      // 4 Barn Doors
      const doorMat = new THREE.MeshStandardMaterial({ color: 0x0f141c, roughness: 0.5 });
      // Top Door
      const topDoor = new THREE.Mesh(new THREE.BoxGeometry(0.24, 0.14, 0.005), doorMat);
      topDoor.position.set(0, 0.17, 0.22);
      topDoor.rotation.x = -0.55;
      headGroup.add(topDoor);

      // Bottom Door
      const btmDoor = new THREE.Mesh(new THREE.BoxGeometry(0.24, 0.14, 0.005), doorMat);
      btmDoor.position.set(0, -0.17, 0.22);
      btmDoor.rotation.x = 0.55;
      headGroup.add(btmDoor);

      // Left Door
      const leftDoor = new THREE.Mesh(new THREE.BoxGeometry(0.14, 0.24, 0.005), doorMat);
      leftDoor.position.set(-0.17, 0, 0.22);
      leftDoor.rotation.y = 0.55;
      headGroup.add(leftDoor);

      // Right Door
      const rightDoor = new THREE.Mesh(new THREE.BoxGeometry(0.14, 0.24, 0.005), doorMat);
      rightDoor.position.set(0.17, 0, 0.22);
      rightDoor.rotation.y = -0.55;
      headGroup.add(rightDoor);

      g.add(headGroup);
      return g;
    },
  },

  // 10. 影视绿幕抠像背景架 (Green Screen Backdrop)
  {
    id: "greenscreen_backdrop",
    name: "影视绿幕背景架",
    category: "architecture",
    categoryZh: "建筑空间",
    icon: "🟩",
    description: "3米宽标准视效绿幕布景系统，含两端重型伸缩支架与横杆，用于后期抠像与合成视效构图预演",
    build: () => {
      const g = new THREE.Group();
      g.name = "GreenScreen";
      const standMat = new THREE.MeshStandardMaterial({ color: 0x1f2937, roughness: 0.4, metalness: 0.7 });
      const chromaMat = new THREE.MeshStandardMaterial({ color: 0x00c853, roughness: 0.9 });

      // Left & Right Vertical Stands
      [-1.5, 1.5].forEach((x) => {
        const pole = new THREE.Mesh(new THREE.CylinderGeometry(0.02, 0.02, 2.5, 8), standMat);
        pole.position.set(x, 1.25, 0);
        g.add(pole);

        // Tripod foot
        const foot = new THREE.Mesh(new THREE.CylinderGeometry(0.015, 0.015, 0.6, 6), standMat);
        foot.position.set(x, 0.15, 0);
        foot.rotation.z = Math.PI / 2;
        g.add(foot);
      });

      // Top Crossbar
      const bar = new THREE.Mesh(new THREE.CylinderGeometry(0.022, 0.022, 3.1, 10), standMat);
      bar.rotation.z = Math.PI / 2;
      bar.position.set(0, 2.45, 0);
      g.add(bar);

      // Green Screen Fabric
      const cloth = new THREE.Mesh(new THREE.PlaneGeometry(2.96, 2.4), chromaMat);
      cloth.position.set(0, 1.22, 0.01);
      cloth.castShadow = true;
      g.add(cloth);

      return g;
    },
  },

  // 11. 手枪武器道具 (Prop Handgun)
  {
    id: "prop_handgun",
    name: "手枪动作道具",
    category: "narrative",
    categoryZh: "叙事动作",
    icon: "🔫",
    description: "标准半自动手枪动作道具，用于警匪、悬疑、动作戏中的人物持枪瞄准走位与对峙视线设计",
    build: () => {
      const g = new THREE.Group();
      g.name = "PropGun";
      const gunMat = new THREE.MeshStandardMaterial({ color: 0x1e252e, roughness: 0.25, metalness: 0.8 });
      const gripMat = new THREE.MeshStandardMaterial({ color: 0x11161d, roughness: 0.9 });

      // Slide & Barrel (0.20m x 0.04m x 0.032m)
      const slide = new THREE.Mesh(new THREE.BoxGeometry(0.2, 0.042, 0.032), gunMat);
      slide.position.set(0, 0.12, 0);
      slide.castShadow = true;
      g.add(slide);

      // Frame / Receiver
      const frame = new THREE.Mesh(new THREE.BoxGeometry(0.18, 0.025, 0.03), gunMat);
      frame.position.set(0.01, 0.088, 0);
      g.add(frame);

      // Pistol Grip (angled ~15 degrees)
      const grip = new THREE.Mesh(new THREE.BoxGeometry(0.045, 0.095, 0.032), gripMat);
      grip.position.set(-0.055, 0.045, 0);
      grip.rotation.z = -0.26;
      g.add(grip);

      // Trigger Guard
      const guard = new THREE.Mesh(new THREE.BoxGeometry(0.04, 0.035, 0.012), gunMat);
      guard.position.set(0.01, 0.065, 0);
      g.add(guard);

      return g;
    },
  },

  // 12. 智能手机道具 (Smartphone)
  {
    id: "prop_smartphone",
    name: "智能手机",
    category: "narrative",
    categoryZh: "叙事动作",
    icon: "📱",
    description: "现代戏最核心的手持道具，用于人物低头看消息、接打电话、录制视频或视线低头动作设计",
    build: () => {
      const g = new THREE.Group();
      g.name = "Smartphone";
      const bodyMat = new THREE.MeshStandardMaterial({ color: 0x1a202c, roughness: 0.2, metalness: 0.85 });
      const screenMat = new THREE.MeshStandardMaterial({
        color: 0x0f172a,
        roughness: 0.1,
        emissive: 0x1e3a8a,
        emissiveIntensity: 0.4,
      });

      // Phone body (0.075m x 0.155m x 0.009m)
      const phone = new THREE.Mesh(new THREE.BoxGeometry(0.075, 0.155, 0.009), bodyMat);
      phone.position.set(0, 0.078, 0);
      g.add(phone);

      // Glowing glass front screen
      const screen = new THREE.Mesh(new THREE.PlaneGeometry(0.07, 0.145), screenMat);
      screen.position.set(0, 0.078, 0.005);
      g.add(screen);

      // Camera Bump
      const cam = new THREE.Mesh(new THREE.BoxGeometry(0.028, 0.032, 0.004), bodyMat);
      cam.position.set(0.02, 0.135, -0.005);
      g.add(cam);

      return g;
    },
  },

  // 13. 手提公文包 (Briefcase)
  {
    id: "prop_briefcase",
    name: "手提商务公文包",
    category: "narrative",
    categoryZh: "叙事动作",
    icon: "💼",
    description: "商务/悬疑电影经典道具，用于人物出入画提拿、桌面交接交易、手部动作充实",
    build: () => {
      const g = new THREE.Group();
      g.name = "Briefcase";
      const leatherMat = new THREE.MeshStandardMaterial({ color: 0x2b1d14, roughness: 0.5 });
      const brassMat = new THREE.MeshStandardMaterial({ color: 0xd4af37, roughness: 0.25, metalness: 0.85 });

      // Main suitcase body (0.44m x 0.32m x 0.11m)
      const body = new THREE.Mesh(new THREE.BoxGeometry(0.44, 0.32, 0.11), leatherMat);
      body.position.set(0, 0.16, 0);
      body.castShadow = true;
      g.add(body);

      // Dual Brass Locks
      [-0.12, 0.12].forEach((x) => {
        const lock = new THREE.Mesh(new THREE.BoxGeometry(0.03, 0.04, 0.114), brassMat);
        lock.position.set(x, 0.28, 0);
        g.add(lock);
      });

      // Center Handle
      const handle = new THREE.Mesh(new THREE.TorusGeometry(0.05, 0.012, 8, 16, Math.PI), leatherMat);
      handle.position.set(0, 0.32, 0);
      handle.rotation.z = Math.PI;
      g.add(handle);

      return g;
    },
  },

  // 14. 随行咖啡杯 (Coffee Mug)
  {
    id: "prop_coffee_mug",
    name: "陶瓷咖啡杯",
    category: "narrative",
    categoryZh: "叙事动作",
    icon: "☕",
    description: "对白戏与生活戏高频手持道具，用于人物视线停顿、掩饰情绪、喝水动作节奏切分",
    build: () => {
      const g = new THREE.Group();
      g.name = "CoffeeMug";
      const ceramicMat = new THREE.MeshStandardMaterial({ color: 0xf8fafc, roughness: 0.15 });
      const coffeeMat = new THREE.MeshStandardMaterial({ color: 0x3e2723, roughness: 0.2 });

      // Cup Cylinder (r=0.045m, h=0.10m)
      const cup = new THREE.Mesh(new THREE.CylinderGeometry(0.045, 0.042, 0.1, 16), ceramicMat);
      cup.position.set(0, 0.05, 0);
      cup.castShadow = true;
      g.add(cup);

      // Inner liquid
      const coffee = new THREE.Mesh(new THREE.CircleGeometry(0.04, 16), coffeeMat);
      coffee.position.set(0, 0.088, 0);
      coffee.rotation.x = -Math.PI / 2;
      g.add(coffee);

      // Handle (Torus)
      const handle = new THREE.Mesh(new THREE.TorusGeometry(0.026, 0.007, 8, 16), ceramicMat);
      handle.position.set(0.055, 0.05, 0);
      handle.rotation.y = Math.PI / 2;
      g.add(handle);

      return g;
    },
  },
];

async function main() {
  const publicDir = path.resolve(process.cwd(), "public/models");
  fs.mkdirSync(publicDir, { recursive: true });

  const generatedList: any[] = [];

  console.log(`Generating ${definitions.length} professional Director & Cinematography 3D model assets...`);

  for (const def of definitions) {
    const group = def.build();
    const box = new THREE.Box3().setFromObject(group);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());

    const glbBuffer = await exportGLB(group);
    const glbPath = path.join(publicDir, `${def.id}.glb`);
    fs.writeFileSync(glbPath, glbBuffer);

    const base64Data = `data:model/gltf-binary;base64,${glbBuffer.toString("base64")}`;

    generatedList.push({
      id: def.id,
      name: def.name,
      category: def.category,
      categoryZh: def.categoryZh,
      icon: def.icon,
      description: def.description,
      size: [Number(size.x.toFixed(2)), Number(size.y.toFixed(2)), Number(size.z.toFixed(2))],
      center: [Number(center.x.toFixed(2)), Number(center.y.toFixed(2)), Number(center.z.toFixed(2))],
      boxMinY: Number(box.min.y.toFixed(2)),
      dataUri: base64Data,
      fileSizeKb: Number((glbBuffer.length / 1024).toFixed(1)),
    });

    console.log(`✓ [${def.categoryZh}] ${def.name} (${def.id}.glb): ${glbBuffer.length} bytes, size: ${size.x.toFixed(2)}x${size.y.toFixed(2)}x${size.z.toFixed(2)}m`);
  }

  // Update src/builtinModels.ts
  const tsContent = `// Automatically generated professional Director & Cinematography 3D models catalog
import { type V3 } from "./core";

export interface PresetModel {
  id: string;
  name: string;
  category: "filming_gear" | "lighting" | "blocking" | "narrative" | "architecture";
  categoryZh: string;
  icon: string;
  description: string;
  size: V3;
  center: V3;
  boxMinY: number;
  dataUri: string;
  fileSizeKb: number;
}

export const presetModels: PresetModel[] = ${JSON.stringify(generatedList, null, 2)};

export function getPresetModel(id: string): PresetModel | undefined {
  return presetModels.find((m) => m.id === id);
}
`;

  const tsPath = path.resolve(process.cwd(), "src/builtinModels.ts");
  fs.writeFileSync(tsPath, tsContent, "utf-8");
  console.log(`\nGenerated Director Models catalog at ${tsPath}`);
}

main().catch(console.error);
