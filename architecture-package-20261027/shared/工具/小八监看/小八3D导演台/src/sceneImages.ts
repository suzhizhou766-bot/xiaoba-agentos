import { TextureLoader, SRGBColorSpace, type Texture } from 'three';
import { uuid, type SceneImage } from './core';
export const imageTextures = new Map<string, Texture>();
export async function prepareImage(image: SceneImage) {
  if (imageTextures.has(image.id)) return;
  const texture = await new TextureLoader().loadAsync(image.data);
  texture.colorSpace = SRGBColorSpace;
  imageTextures.set(image.id, texture);
}
export async function readSceneImages(files: File[]): Promise<SceneImage[]> {
  return Promise.all(files.map(async file => {
    if (!/\.(png|jpe?g)$/i.test(file.name)) throw Error('场景图片支持 JPG / PNG');
    if (file.size > 20 * 1024 * 1024) throw Error('单张图片请控制在 20MB 内');
    const bytes = new Uint8Array(await file.arrayBuffer());
    const png = bytes[0] === 137 && bytes[1] === 80 && bytes[2] === 78 && bytes[3] === 71;
    const jpeg = bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255;
    if (!png && !jpeg) throw Error('图片内容不是有效的 PNG / JPEG');
    const mime = png ? 'image/png' : 'image/jpeg';
    const data = await new Promise<string>((resolve,reject) => {
      const reader = new FileReader(); reader.onload=()=>resolve(reader.result as string); reader.onerror=()=>reject(Error('图片读取失败'));
      reader.readAsDataURL(new Blob([bytes], {type:mime}));
    });
    const bitmap = await createImageBitmap(new Blob([bytes],{type:mime}));
    const width=bitmap.width,height=bitmap.height; bitmap.close();
    if (width>8192 || height>8192) throw Error('图片最长边请缩小到 8192 像素以内');
    const image: SceneImage={id:uuid(),name:file.name,data,width,height};
    await prepareImage(image); return image;
  }));
}
export const imageFilename = (image: SceneImage) => `scene-images/${image.id}.${image.data.startsWith('data:image/png')?'png':'jpg'}`;
export function sceneImageFiles(images: SceneImage[]): Record<string,Uint8Array> {
  return Object.fromEntries(images.map(image=>[imageFilename(image),Uint8Array.from(atob(image.data.split(',')[1]),c=>c.charCodeAt(0))]));
}
