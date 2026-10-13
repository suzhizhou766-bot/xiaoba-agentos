import { actor } from "./core";
import { boardFor, defaultBoard } from "./spatial";
import { useRef } from 'react';
import { active, useStore } from './store';
export function SceneImagesPanel({onImport,busy}:{onImport:(files:File[])=>void;busy:boolean}) {
  const input=useRef<HTMLInputElement>(null), s=useStore(), shot=active(s.project);
  const images=(s.project.sceneImages??[]).filter(image=>shot.sceneImageIds?.includes(image.id));
  return <section className="scene-images" aria-label="场景图片">
    <div className="tree-label">场景图片 <span>{images.length}</span></div>
    <button disabled={busy} onClick={()=>input.current?.click()}>＋ 添加场景图片</button>
    <input ref={input} hidden type="file" accept="image/png,image/jpeg,.png,.jpg,.jpeg" multiple onChange={e=>{onImport(Array.from(e.target.files??[]));e.target.value='';}} />
    <p>JPG / PNG，可拖入页面。每张 ≤20MB。</p>
    {images.map(image=><div className="scene-image-card" key={image.id}>
      <img src={image.data} alt={image.name} />
      <strong title={image.name}>{image.name}</strong>
      <span>{image.width} × {image.height}</span>
      <label>用途<select aria-label={`${image.name} 用途`} value={shot.backgroundImageId===image.id?'background':'reference'} onChange={e=>s.edit(sh=>{sh.backgroundImageId=e.target.value==='background'?image.id:sh.backgroundImageId===image.id?null:sh.backgroundImageId;if(e.target.value==='background')sh.sceneBoard=defaultBoard(image.width/image.height);})}>
        <option value="reference">外观参考</option><option value="background">空间背景板</option>
      </select></label>
      <button aria-label={`移除图片 ${image.name}`} onClick={()=>s.edit(sh=>{sh.sceneImageIds=sh.sceneImageIds?.filter(id=>id!==image.id);if(sh.backgroundImageId===image.id)sh.backgroundImageId=null;})}>从本镜头移除</button>
    </div>)}
    {shot.backgroundImageId&&<>
      <p>背景固定在片场。先调整尺寸、位置与朝向，使图片地面接近舞台地面；用道具补充前景遮挡。</p>
      {([['中心 X',0],['中心 Y',1],['中心 Z',2]] as const).map(([label,axis])=><label key={label}>{label}<input aria-label={`背景板${label}`} type="number" step="0.1" value={boardFor(shot).position[axis]} onChange={e=>{const n=e.currentTarget.valueAsNumber;if(Number.isFinite(n))s.edit(sh=>{const b=sh.sceneBoard??defaultBoard();b.position[axis]=n;b.confirmed=false;sh.sceneBoard=b;});}} /></label>)}
      {(['width','height','yaw','pitch'] as const).map(field=><label key={field}>{{width:'宽度 / m',height:'高度 / m',yaw:'朝向 / °',pitch:'俯仰 / °'}[field]}<input aria-label={`背景板${field}`} type="number" step="0.1" value={field==='yaw'||field==='pitch'?Math.round((boardFor(shot)[field]??0)*180/Math.PI*100)/100:boardFor(shot)[field]} onChange={e=>{const n=e.currentTarget.valueAsNumber;if(Number.isFinite(n)&&(field==='yaw'||field==='pitch'||n>0&&n<=1000))s.edit(sh=>{const b=sh.sceneBoard??defaultBoard();b[field]=field==='yaw'||field==='pitch'?n*Math.PI/180:n;b.confirmed=false;sh.sceneBoard=b;});}} /></label>)}
      <label>图片地平线位置 / %
        <input aria-label="背景板地平线" type="range" min="0" max="100" step="1" value={Math.round((boardFor(shot).horizonRatio??0.5)*100)} onChange={e=>{const r=Number(e.target.value)/100;s.edit(sh=>{const b=sh.sceneBoard??defaultBoard();b.horizonRatio=r;b.confirmed=false;sh.sceneBoard=b;});}} />
        <span>{Math.round((boardFor(shot).horizonRatio??0.5)*100)}%</span>
      </label>
      <div className="btn-group" style={{display:'flex',gap:'4px',flexWrap:'wrap',margin:'4px 0'}}>
        <button onClick={()=>{const b=boardFor(shot),aspect=16/9;const distance=Math.max(b.width,b.height*aspect)*shot.camera.focalLength/shot.camera.sensorWidth;s.camera({position:[b.position[0]+Math.sin(b.yaw)*distance,b.position[1],b.position[2]+Math.cos(b.yaw)*distance],target:[...b.position],lookAt:null,roll:0});s.set({selected:'camera',notice:'相机已正对背景板；这是构图起点，仍需校准地面及站位'});}}>正对背景板</button>
        <button onClick={()=>{const b=boardFor(shot);const pitch=b.pitch??0;const targetY=(b.height/2)*Math.cos(pitch);s.edit(sh=>{const cur=sh.sceneBoard??defaultBoard();cur.position[1]=targetY;cur.confirmed=false;sh.sceneBoard=cur;});s.set({notice:'已将背景板底边贴齐地面 Y=0'});}}>底边贴地</button>
        <button onClick={()=>{const b=boardFor(shot);const pitch=b.pitch??0;const ratio=b.horizonRatio??0.5;const camY=shot.camera.position[1];const targetY=camY-(ratio-0.5)*b.height*Math.cos(pitch);s.edit(sh=>{const cur=sh.sceneBoard??defaultBoard();cur.position[1]=targetY;cur.confirmed=false;sh.sceneBoard=cur;});s.set({notice:'已对齐图片地平线与机位高度'});}}>地平线对齐机位</button>
      </div>
      <label><input type="checkbox" checked={boardFor(shot).confirmed} onChange={e=>s.edit(sh=>{sh.sceneBoard={...boardFor(sh),confirmed:e.target.checked};})} />我已校准此简化空间</label>
      <p>单图内部深度尚未重建；摄影机越过图片边界会在监看与时间轴提示。</p>
    </>}
    <button onClick={()=>{s.set({editorView:'stage',groundPlacement:!s.groundPlacement,notice:'选中角色或道具后，点击地面或任意道具表面确定站位'});}}>{s.groundPlacement?'取消放置模式':'在地面/道具表面放置选中对象'}</button>
    <button onClick={()=>{const o={...actor('遮挡物 · 请命名',[0,0,-2],'#637d86'),kind:'prop' as const,size:[1,3,0.4] as [number,number,number],center:[0,1.5,0] as [number,number,number]};s.edit(sh=>sh.objects.push(o));s.set({selected:o.id,editorView:'stage',notice:'已添加遮挡代理；在对象属性调整位置、旋转与尺寸'});}}>＋ 添加空间遮挡物</button>
    {!!images.length&&<p>原图随项目保存，随交接包导出。</p>}
  </section>;
}
