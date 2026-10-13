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

export interface ModelDef {
  id: string;
  name: string;
  category: "furniture" | "architecture" | "staging" | "vehicle";
  categoryZh: string;
  icon: string;
  description: string;
  build: () => THREE.Group;
}

const definitions: ModelDef[] = [
  {
    id: "chair_modern",
    name: "靠背单人椅",
    category: "furniture",
    categoryZh: "家具置景",
    icon: "🪑",
    description: "标准单人靠背椅，带座垫与人体工学靠背，适合室内人物坐姿与交谈对戏",
    build: () => {
      const g = new THREE.Group();
      g.name = "Chair";
      const seatMat = new THREE.MeshStandardMaterial({ color: 0x334e68, roughness: 0.6 });
      const woodMat = new THREE.MeshStandardMaterial({ color: 0x82542a, roughness: 0.5 });

      // Seat
      const seat = new THREE.Mesh(new THREE.BoxGeometry(0.48, 0.05, 0.46), seatMat);
      seat.position.set(0, 0.44, 0);
      seat.castShadow = true;
      g.add(seat);

      // Legs
      const legGeo = new THREE.CylinderGeometry(0.02, 0.016, 0.44, 10);
      [[-0.19, -0.18], [0.19, -0.18], [-0.19, 0.18], [0.19, 0.18]].forEach(([x, z]) => {
        const leg = new THREE.Mesh(legGeo, woodMat);
        leg.position.set(x, 0.22, z);
        leg.castShadow = true;
        g.add(leg);
      });

      // Back posts
      const postGeo = new THREE.CylinderGeometry(0.018, 0.018, 0.42, 10);
      [-0.19, 0.19].forEach((x) => {
        const post = new THREE.Mesh(postGeo, woodMat);
        post.position.set(x, 0.65, -0.19);
        g.add(post);
      });

      // Backrest
      const back = new THREE.Mesh(new THREE.BoxGeometry(0.44, 0.22, 0.04), seatMat);
      back.position.set(0, 0.74, -0.19);
      back.castShadow = true;
      g.add(back);

      return g;
    },
  },
  {
    id: "desk_office",
    name: "实木办公桌",
    category: "furniture",
    categoryZh: "家具置景",
    icon: "🪵",
    description: "标准现代长条形写字桌/会议桌，带稳固桌腿与挡板，表面可放置物体",
    build: () => {
      const g = new THREE.Group();
      g.name = "Desk";
      const topMat = new THREE.MeshStandardMaterial({ color: 0x6e4726, roughness: 0.4 });
      const legMat = new THREE.MeshStandardMaterial({ color: 0x242d38, roughness: 0.5, metalness: 0.4 });

      // Desktop
      const top = new THREE.Mesh(new THREE.BoxGeometry(1.5, 0.05, 0.8), topMat);
      top.position.set(0, 0.725, 0);
      top.castShadow = true;
      g.add(top);

      // 4 Legs
      const legGeo = new THREE.BoxGeometry(0.06, 0.7, 0.06);
      [[-0.68, -0.34], [0.68, -0.34], [-0.68, 0.34], [0.68, 0.34]].forEach(([x, z]) => {
        const leg = new THREE.Mesh(legGeo, legMat);
        leg.position.set(x, 0.35, z);
        leg.castShadow = true;
        g.add(leg);
      });

      // Back crossbeam / modesty panel
      const panel = new THREE.Mesh(new THREE.BoxGeometry(1.36, 0.35, 0.02), topMat);
      panel.position.set(0, 0.48, -0.33);
      g.add(panel);

      return g;
    },
  },
  {
    id: "sofa_double",
    name: "双人现代沙发",
    category: "furniture",
    categoryZh: "家具置景",
    icon: "🛋️",
    description: "现代客厅双人沙发，带独立座垫、厚实靠背与舒适扶手",
    build: () => {
      const g = new THREE.Group();
      g.name = "Sofa";
      const fabricMat = new THREE.MeshStandardMaterial({ color: 0x2c3e50, roughness: 0.8 });
      const cushionMat = new THREE.MeshStandardMaterial({ color: 0x34495e, roughness: 0.75 });
      const feetMat = new THREE.MeshStandardMaterial({ color: 0x1a202c, roughness: 0.5 });

      // Base plinth
      const base = new THREE.Mesh(new THREE.BoxGeometry(1.8, 0.18, 0.86), fabricMat);
      base.position.set(0, 0.13, 0);
      g.add(base);

      // 4 Feet
      const footGeo = new THREE.CylinderGeometry(0.025, 0.018, 0.06, 8);
      [[-0.8, -0.35], [0.8, -0.35], [-0.8, 0.35], [0.8, 0.35]].forEach(([x, z]) => {
        const foot = new THREE.Mesh(footGeo, feetMat);
        foot.position.set(x, 0.03, z);
        g.add(foot);
      });

      // 2 Cushions
      const cushionGeo = new THREE.BoxGeometry(0.72, 0.16, 0.62);
      [-0.4, 0.4].forEach((x) => {
        const cushion = new THREE.Mesh(cushionGeo, cushionMat);
        cushion.position.set(x, 0.28, 0.06);
        cushion.castShadow = true;
        g.add(cushion);
      });

      // Backrest
      const back = new THREE.Mesh(new THREE.BoxGeometry(1.8, 0.45, 0.22), fabricMat);
      back.position.set(0, 0.54, -0.32);
      back.castShadow = true;
      g.add(back);

      // Armrests
      const armGeo = new THREE.BoxGeometry(0.16, 0.38, 0.86);
      [-0.82, 0.82].forEach((x) => {
        const arm = new THREE.Mesh(armGeo, fabricMat);
        arm.position.set(x, 0.38, 0);
        arm.castShadow = true;
        g.add(arm);
      });

      return g;
    },
  },
  {
    id: "coffee_table",
    name: "咖啡圆茶几",
    category: "furniture",
    categoryZh: "家具置景",
    icon: "☕",
    description: "休闲圆形矮茶几，实木圆台面搭配金属中央立柱与稳定底盘",
    build: () => {
      const g = new THREE.Group();
      g.name = "CoffeeTable";
      const woodMat = new THREE.MeshStandardMaterial({ color: 0xb58450, roughness: 0.4 });
      const metalMat = new THREE.MeshStandardMaterial({ color: 0x1f242d, roughness: 0.3, metalness: 0.7 });

      // Tabletop
      const top = new THREE.Mesh(new THREE.CylinderGeometry(0.4, 0.4, 0.04, 24), woodMat);
      top.position.set(0, 0.46, 0);
      top.castShadow = true;
      g.add(top);

      // Central Pillar
      const pillar = new THREE.Mesh(new THREE.CylinderGeometry(0.04, 0.04, 0.42, 16), metalMat);
      pillar.position.set(0, 0.23, 0);
      g.add(pillar);

      // Base plate
      const base = new THREE.Mesh(new THREE.CylinderGeometry(0.25, 0.26, 0.02, 24), metalMat);
      base.position.set(0, 0.01, 0);
      g.add(base);

      return g;
    },
  },
  {
    id: "bed_single",
    name: "单人床与枕头",
    category: "furniture",
    categoryZh: "家具置景",
    icon: "🛏️",
    description: "标准宿舍/卧室单人床，含木质床架、饱满床垫与蓬松枕头",
    build: () => {
      const g = new THREE.Group();
      g.name = "Bed";
      const frameMat = new THREE.MeshStandardMaterial({ color: 0x5a3e26, roughness: 0.5 });
      const linenMat = new THREE.MeshStandardMaterial({ color: 0xf1f2f6, roughness: 0.8 });
      const pillowMat = new THREE.MeshStandardMaterial({ color: 0xdfe4ea, roughness: 0.9 });

      // Frame
      const frame = new THREE.Mesh(new THREE.BoxGeometry(2.05, 0.16, 1.1), frameMat);
      frame.position.set(0, 0.18, 0);
      g.add(frame);

      // 4 Legs
      const legGeo = new THREE.CylinderGeometry(0.035, 0.03, 0.12, 8);
      [[-0.95, -0.48], [0.95, -0.48], [-0.95, 0.48], [0.95, 0.48]].forEach(([x, z]) => {
        const leg = new THREE.Mesh(legGeo, frameMat);
        leg.position.set(x, 0.06, z);
        g.add(leg);
      });

      // Headboard
      const headboard = new THREE.Mesh(new THREE.BoxGeometry(0.06, 0.75, 1.1), frameMat);
      headboard.position.set(-1.0, 0.48, 0);
      headboard.castShadow = true;
      g.add(headboard);

      // Mattress
      const mattress = new THREE.Mesh(new THREE.BoxGeometry(1.95, 0.22, 1.02), linenMat);
      mattress.position.set(0, 0.35, 0);
      mattress.castShadow = true;
      g.add(mattress);

      // Pillow
      const pillow = new THREE.Mesh(new THREE.BoxGeometry(0.38, 0.1, 0.65), pillowMat);
      pillow.position.set(-0.7, 0.51, 0);
      pillow.rotation.z = -0.08;
      g.add(pillow);

      return g;
    },
  },
  {
    id: "door_frame",
    name: "门与门框",
    category: "architecture",
    categoryZh: "建筑空间",
    icon: "🚪",
    description: "标准门套与微敞开门扇，带黄铜金属门把手，用于进出入画与构图遮挡",
    build: () => {
      const g = new THREE.Group();
      g.name = "Door";
      const frameMat = new THREE.MeshStandardMaterial({ color: 0xd2d7df, roughness: 0.4 });
      const doorMat = new THREE.MeshStandardMaterial({ color: 0x4a2e18, roughness: 0.5 });
      const goldMat = new THREE.MeshStandardMaterial({ color: 0xd4af37, roughness: 0.2, metalness: 0.8 });

      // Left Jamb
      const leftJamb = new THREE.Mesh(new THREE.BoxGeometry(0.08, 2.15, 0.14), frameMat);
      leftJamb.position.set(-0.48, 1.075, 0);
      g.add(leftJamb);

      // Right Jamb
      const rightJamb = new THREE.Mesh(new THREE.BoxGeometry(0.08, 2.15, 0.14), frameMat);
      rightJamb.position.set(0.48, 1.075, 0);
      g.add(rightJamb);

      // Top Lintel
      const lintel = new THREE.Mesh(new THREE.BoxGeometry(1.04, 0.08, 0.14), frameMat);
      lintel.position.set(0, 2.115, 0);
      g.add(lintel);

      // Door panel - hinged at left, open 25 degrees
      const doorPivot = new THREE.Group();
      doorPivot.position.set(-0.44, 0, -0.02);
      doorPivot.rotation.y = Math.PI * 0.14; // ~25 deg open

      const panel = new THREE.Mesh(new THREE.BoxGeometry(0.88, 2.03, 0.04), doorMat);
      panel.position.set(0.44, 1.015, 0);
      panel.castShadow = true;
      doorPivot.add(panel);

      // Handle
      const handleBase = new THREE.Mesh(new THREE.CylinderGeometry(0.02, 0.02, 0.08, 8), goldMat);
      handleBase.rotation.x = Math.PI / 2;
      handleBase.position.set(0.78, 1.0, 0.04);
      doorPivot.add(handleBase);

      const handleLever = new THREE.Mesh(new THREE.BoxGeometry(0.12, 0.02, 0.02), goldMat);
      handleLever.position.set(0.82, 1.0, 0.08);
      doorPivot.add(handleLever);

      g.add(doorPivot);
      return g;
    },
  },
  {
    id: "partition_wall",
    name: "室内隔断墙",
    category: "architecture",
    categoryZh: "建筑空间",
    icon: "🧱",
    description: "2.4米宽写实隔断墙面，上下含装饰踢脚线与收口顶线，用于空间纵深遮挡",
    build: () => {
      const g = new THREE.Group();
      g.name = "PartitionWall";
      const wallMat = new THREE.MeshStandardMaterial({ color: 0xded8cb, roughness: 0.9 });
      const trimMat = new THREE.MeshStandardMaterial({ color: 0x4a4339, roughness: 0.6 });

      // Main wall
      const wall = new THREE.Mesh(new THREE.BoxGeometry(2.4, 2.14, 0.12), wallMat);
      wall.position.set(0, 1.13, 0);
      wall.castShadow = true;
      g.add(wall);

      // Bottom Skirt / Baseboard
      const baseboard = new THREE.Mesh(new THREE.BoxGeometry(2.44, 0.12, 0.16), trimMat);
      baseboard.position.set(0, 0.06, 0);
      g.add(baseboard);

      // Top Trim
      const topTrim = new THREE.Mesh(new THREE.BoxGeometry(2.44, 0.06, 0.16), trimMat);
      topTrim.position.set(0, 2.23, 0);
      g.add(topTrim);

      return g;
    },
  },
  {
    id: "crate_cargo",
    name: "工业货运木箱",
    category: "staging",
    categoryZh: "置景道具",
    icon: "📦",
    description: "经典工业运输木箱，四面带交叉木板加固肋，可作为站立、掩护或坐靠置景",
    build: () => {
      const g = new THREE.Group();
      g.name = "Crate";
      const woodMat = new THREE.MeshStandardMaterial({ color: 0xad7a43, roughness: 0.7 });
      const braceMat = new THREE.MeshStandardMaterial({ color: 0x82542a, roughness: 0.6 });
      const cornerMat = new THREE.MeshStandardMaterial({ color: 0x22272e, roughness: 0.4, metalness: 0.6 });

      // Core box
      const box = new THREE.Mesh(new THREE.BoxGeometry(0.78, 0.78, 0.78), woodMat);
      box.position.set(0, 0.4, 0);
      box.castShadow = true;
      g.add(box);

      // Cross braces on 4 vertical sides
      const braceGeo = new THREE.BoxGeometry(0.08, 1.05, 0.02);
      [0, Math.PI / 2, Math.PI, -Math.PI / 2].forEach((rotY) => {
        const braceGroup = new THREE.Group();
        braceGroup.rotation.y = rotY;

        const b1 = new THREE.Mesh(braceGeo, braceMat);
        b1.position.set(0, 0.4, 0.395);
        b1.rotation.z = Math.PI / 4;
        braceGroup.add(b1);

        const b2 = new THREE.Mesh(braceGeo, braceMat);
        b2.position.set(0, 0.4, 0.395);
        b2.rotation.z = -Math.PI / 4;
        braceGroup.add(b2);

        g.add(braceGroup);
      });

      // Corner protectors
      const cornerGeo = new THREE.BoxGeometry(0.09, 0.09, 0.09);
      [[-0.38, 0.05, -0.38], [0.38, 0.05, -0.38], [-0.38, 0.05, 0.38], [0.38, 0.05, 0.38],
       [-0.38, 0.75, -0.38], [0.38, 0.75, -0.38], [-0.38, 0.75, 0.38], [0.38, 0.75, 0.38]].forEach(([x, y, z]) => {
        const corner = new THREE.Mesh(cornerGeo, cornerMat);
        corner.position.set(x, y, z);
        g.add(corner);
      });

      return g;
    },
  },
  {
    id: "oil_drum",
    name: "工业金属油桶",
    category: "staging",
    categoryZh: "置景道具",
    icon: "🛢️",
    description: "标准工业金属圆桶，带双道外凸环肋加固与顶盖注油密封孔",
    build: () => {
      const g = new THREE.Group();
      g.name = "OilDrum";
      const barrelMat = new THREE.MeshStandardMaterial({ color: 0x1f5f8b, roughness: 0.4, metalness: 0.5 });
      const rimMat = new THREE.MeshStandardMaterial({ color: 0x486581, roughness: 0.3, metalness: 0.6 });

      // Body
      const body = new THREE.Mesh(new THREE.CylinderGeometry(0.29, 0.29, 0.9, 24), barrelMat);
      body.position.set(0, 0.46, 0);
      body.castShadow = true;
      g.add(body);

      // Top & Bottom Rims
      const rimTop = new THREE.Mesh(new THREE.CylinderGeometry(0.3, 0.3, 0.03, 24), rimMat);
      rimTop.position.set(0, 0.91, 0);
      g.add(rimTop);

      const rimBottom = new THREE.Mesh(new THREE.CylinderGeometry(0.3, 0.3, 0.03, 24), rimMat);
      rimBottom.position.set(0, 0.02, 0);
      g.add(rimBottom);

      // Ribs
      [0.32, 0.62].forEach((y) => {
        const rib = new THREE.Mesh(new THREE.CylinderGeometry(0.302, 0.302, 0.035, 24), rimMat);
        rib.position.set(0, y, 0);
        g.add(rib);
      });

      // Bung Cap
      const cap = new THREE.Mesh(new THREE.CylinderGeometry(0.028, 0.028, 0.02, 12), rimMat);
      cap.position.set(0.14, 0.93, 0);
      g.add(cap);

      return g;
    },
  },
  {
    id: "podium_stand",
    name: "演讲台 / 吧台",
    category: "staging",
    categoryZh: "置景道具",
    icon: "🎙️",
    description: "会议发言或接待吧台，台身微梯形稳重造型，带倾斜防落文档台板",
    build: () => {
      const g = new THREE.Group();
      g.name = "Podium";
      const woodMat = new THREE.MeshStandardMaterial({ color: 0x3d271d, roughness: 0.4 });
      const goldMat = new THREE.MeshStandardMaterial({ color: 0xc89d42, roughness: 0.2, metalness: 0.7 });

      // Base
      const base = new THREE.Mesh(new THREE.BoxGeometry(0.75, 0.06, 0.55), woodMat);
      base.position.set(0, 0.03, 0);
      g.add(base);

      // Tapered column
      const col = new THREE.Mesh(new THREE.BoxGeometry(0.58, 0.96, 0.42), woodMat);
      col.position.set(0, 0.54, 0);
      col.castShadow = true;
      g.add(col);

      // Front decorative emblem band
      const band = new THREE.Mesh(new THREE.BoxGeometry(0.6, 0.04, 0.44), goldMat);
      band.position.set(0, 0.85, 0);
      g.add(band);

      // Slanted top reading surface
      const topGroup = new THREE.Group();
      topGroup.position.set(0, 1.05, 0);
      topGroup.rotation.x = 0.22; // ~12 deg slant

      const top = new THREE.Mesh(new THREE.BoxGeometry(0.72, 0.04, 0.52), woodMat);
      top.position.set(0, 0, 0);
      topGroup.add(top);

      const lip = new THREE.Mesh(new THREE.BoxGeometry(0.72, 0.03, 0.02), goldMat);
      lip.position.set(0, 0.025, 0.25);
      topGroup.add(lip);

      g.add(topGroup);
      return g;
    },
  },
  {
    id: "floor_lamp",
    name: "现代落地灯",
    category: "staging",
    categoryZh: "置景道具",
    icon: "💡",
    description: "1.9米现代简约落地灯，金属细杆支撑与向下漫反射圆锥灯罩",
    build: () => {
      const g = new THREE.Group();
      g.name = "FloorLamp";
      const blackMat = new THREE.MeshStandardMaterial({ color: 0x1a202c, roughness: 0.3, metalness: 0.8 });
      const brassMat = new THREE.MeshStandardMaterial({ color: 0xc89d42, roughness: 0.2, metalness: 0.9 });
      const lightMat = new THREE.MeshStandardMaterial({ color: 0xfff7d6, roughness: 0.2, emissive: 0xffe899, emissiveIntensity: 0.5 });

      // Base plate
      const base = new THREE.Mesh(new THREE.CylinderGeometry(0.18, 0.19, 0.03, 20), blackMat);
      base.position.set(0, 0.015, 0);
      g.add(base);

      // Vertical pole
      const pole = new THREE.Mesh(new THREE.CylinderGeometry(0.014, 0.014, 1.6, 12), blackMat);
      pole.position.set(0, 0.83, 0);
      g.add(pole);

      // Joint
      const joint = new THREE.Mesh(new THREE.SphereGeometry(0.025, 12, 12), brassMat);
      joint.position.set(0, 1.63, 0);
      g.add(joint);

      // Arm
      const arm = new THREE.Mesh(new THREE.CylinderGeometry(0.012, 0.012, 0.26, 10), brassMat);
      arm.position.set(0.08, 1.72, 0);
      arm.rotation.z = -Math.PI / 4;
      g.add(arm);

      // Shade
      const shade = new THREE.Mesh(new THREE.ConeGeometry(0.16, 0.18, 20, 1, true), blackMat);
      shade.position.set(0.17, 1.78, 0);
      shade.rotation.z = Math.PI;
      g.add(shade);

      // Bulb
      const bulb = new THREE.Mesh(new THREE.SphereGeometry(0.045, 12, 12), lightMat);
      bulb.position.set(0.17, 1.75, 0);
      g.add(bulb);

      return g;
    },
  },
  {
    id: "car_sedan",
    name: "轿车低模剪影",
    category: "vehicle",
    categoryZh: "载具交通",
    icon: "🚘",
    description: "4.5米三厢轿车低模比例替身，含车身、风挡座舱、4个立体车轮与车灯",
    build: () => {
      const g = new THREE.Group();
      g.name = "SedanCar";
      const bodyMat = new THREE.MeshStandardMaterial({ color: 0x3d4852, roughness: 0.3, metalness: 0.4 });
      const glassMat = new THREE.MeshStandardMaterial({ color: 0x11161d, roughness: 0.1, metalness: 0.9 });
      const tireMat = new THREE.MeshStandardMaterial({ color: 0x1a1d20, roughness: 0.9 });
      const rimMat = new THREE.MeshStandardMaterial({ color: 0x95a5a6, roughness: 0.2, metalness: 0.8 });
      const lightMat = new THREE.MeshStandardMaterial({ color: 0xf1c40f, emissive: 0xf39c12, emissiveIntensity: 0.6 });

      // Lower body
      const lowerBody = new THREE.Mesh(new THREE.BoxGeometry(4.4, 0.52, 1.8), bodyMat);
      lowerBody.position.set(0, 0.54, 0);
      lowerBody.castShadow = true;
      g.add(lowerBody);

      // Cabin / Greenhouse
      const cabin = new THREE.Mesh(new THREE.BoxGeometry(2.1, 0.48, 1.5), glassMat);
      cabin.position.set(-0.25, 1.02, 0);
      cabin.castShadow = true;
      g.add(cabin);

      // Cabin roof cap
      const roof = new THREE.Mesh(new THREE.BoxGeometry(2.12, 0.04, 1.52), bodyMat);
      roof.position.set(-0.25, 1.27, 0);
      g.add(roof);

      // 4 Wheels
      const tireGeo = new THREE.CylinderGeometry(0.32, 0.32, 0.22, 18);
      tireGeo.rotateZ(Math.PI / 2);
      const rimGeo = new THREE.CylinderGeometry(0.2, 0.2, 0.23, 14);
      rimGeo.rotateZ(Math.PI / 2);

      [[-1.3, -0.88], [1.3, -0.88], [-1.3, 0.88], [1.3, 0.88]].forEach(([x, z]) => {
        const wheelGroup = new THREE.Group();
        wheelGroup.position.set(x, 0.32, z);

        const tire = new THREE.Mesh(tireGeo, tireMat);
        wheelGroup.add(tire);

        const rim = new THREE.Mesh(rimGeo, rimMat);
        wheelGroup.add(rim);

        g.add(wheelGroup);
      });

      // Front Headlights
      [-0.6, 0.6].forEach((z) => {
        const light = new THREE.Mesh(new THREE.BoxGeometry(0.04, 0.1, 0.22), lightMat);
        light.position.set(2.21, 0.6, z);
        g.add(light);
      });

      return g;
    },
  },
];

async function main() {
  const publicDir = path.resolve(process.cwd(), "public/models");
  fs.mkdirSync(publicDir, { recursive: true });

  const generatedList: any[] = [];

  console.log(`Generating ${definitions.length} 3D model assets...`);

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

    console.log(`✓ ${def.name} (${def.id}.glb): ${glbBuffer.length} bytes, dimensions ${size.x.toFixed(2)}x${size.y.toFixed(2)}x${size.z.toFixed(2)}m`);
  }

  // Generate src/builtinModels.ts
  const tsContent = `// Automatically generated preset 3D models catalog
import { type V3 } from "./core";

export interface PresetModel {
  id: string;
  name: string;
  category: "furniture" | "architecture" | "staging" | "vehicle";
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
  console.log(`\nGenerated TypeScript catalog at ${tsPath}`);
}

main().catch(console.error);
