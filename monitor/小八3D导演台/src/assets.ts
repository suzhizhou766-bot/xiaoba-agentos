import { Box3, LoadingManager, Vector3, type Group } from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { type Asset, type StageObject, type V3, uuid } from "./core";
export const models = new Map<string, Group>();
const dataURL = (file: File) =>
  new Promise<string>((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result as string);
    r.onerror = reject;
    r.readAsDataURL(file);
  });
export async function decode(a: Asset) {
  const manager = new LoadingManager();
  manager.setURLModifier((url) => {
    if (!url.startsWith("data:") && !url.startsWith("blob:"))
      throw Error("模型含未嵌入资源，请与依赖文件一同导入");
    return url;
  });
  const loader = new GLTFLoader(manager);
  const input =
    a.format === "glb" ? await (await fetch(a.data)).arrayBuffer() : a.data;
  const gltf = await loader.parseAsync(input, "");
  models.set(a.id, gltf.scene);
  return gltf.scene;
}
export async function importModels(files: File[]) {
  const roots = files.filter((f) => /\.(glb|gltf)$/i.test(f.name));
  if (!roots.length)
    throw Error("请选择 GLB 或 GLTF 文件；GLTF 请同时选择贴图和 BIN 文件");
  const assets: Asset[] = [],
    objects: StageObject[] = [];
  for (const file of roots) {
    const format = file.name.toLowerCase().endsWith(".glb") ? "glb" : "gltf";
    let data: string;
    if (format === "glb") data = await dataURL(file);
    else {
      const json = JSON.parse(await file.text());
      for (const item of [...(json.buffers ?? []), ...(json.images ?? [])]) {
        if (item.uri && !item.uri.startsWith("data:")) {
          const name = decodeURIComponent(item.uri).split("/").pop();
          const matches = files.filter((f) => f.name === name);
          if (matches.length !== 1)
            throw Error(
              `缺少或重名资源：${item.uri}。请完整选择资源文件，或导出为 GLB。`,
            );
          item.uri = await dataURL(matches[0]);
        }
      }
      data = JSON.stringify(json);
    }
    const a: Asset = { id: uuid(), name: file.name, data, format };
    const model = await decode(a);
    const box = new Box3().setFromObject(model),
      size = box.getSize(new Vector3()),
      center = box.getCenter(new Vector3());
    if (box.isEmpty()) throw Error("模型没有可显示的几何体");
    assets.push(a);
    objects.push({
      id: uuid(),
      name: file.name,
      kind: "model",
      assetId: a.id,
      position: [0, -box.min.y, 0],
      rotation: [0, 0, 0],
      scale: 1,
      visible: true,
      locked: false,
      color: "#a9bac9",
      size: size.toArray() as V3,
      center: center.toArray() as V3,
    });
  }
  return { assets, objects };
}

/**
 * 垃圾回收未被任何留存项目引用的 3D 模型资产，释放 WebGL 几何体与材质纹理显存
 */
export function gcAssets(activeAssetIds: Iterable<string>) {
  const activeSet = new Set(activeAssetIds);
  for (const [id, group] of models.entries()) {
    if (!activeSet.has(id)) {
      group.traverse((obj: any) => {
        if (obj.geometry) {
          obj.geometry.dispose?.();
        }
        if (obj.material) {
          if (Array.isArray(obj.material)) {
            obj.material.forEach((m: any) => {
              for (const key of Object.keys(m)) {
                if (m[key]?.isTexture) m[key].dispose?.();
              }
              m.dispose?.();
            });
          } else {
            for (const key of Object.keys(obj.material)) {
              if (obj.material[key]?.isTexture) obj.material[key].dispose?.();
            }
            obj.material.dispose?.();
          }
        }
      });
      models.delete(id);
    }
  }
}
