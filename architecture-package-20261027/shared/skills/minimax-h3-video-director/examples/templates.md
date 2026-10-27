# MiniMax H3 典型场景提示词范例模板

本文件汇总了实际测试中验证成功的几套高难度商业视频提示词模板，供直接参考与微调使用。

---

## 模板 1：实景首尾帧运动插值（国庆车顶挥旗呐喊）
* **模式**：首帧接入 `ref_image_0`，尾帧接入 `ref_image_1`。
* **特点**：高机位俯拍追随、物体物理位移、大风吹拂红旗、人物原生中文口型对白。

```text
subject_definitions:
<Picture 1> is the first frame of [Shot 1], capturing an elevated high-angle view of a black SUV driving down a parkway road, with a woman standing through the open sunroof.
<Picture 2> is the last frame of [Shot 1], capturing the elevated rear view of the same black SUV driving forward along the road past the camera toward an intersection gate in a festive motorcade.
<Subject 1> is the black SUV (license plate 豫A G13868) driving smoothly and steadily along the asphalt lane.
<Subject 2> (S1) is the spirited woman standing through the open sunroof, wearing black clothing. Her right hand firmly grips the flagpole of a large vibrant red Chinese national flag fluttering dynamically in the wind, while her left hand waves vigorously and she enthusiastically shouts celebratory cheers.

summary:
[keyframe completion] The target video is a smooth, high-angle celebratory tracking shot bridging <Picture 1> as the first frame to <Picture 2> as the last frame. The black SUV (<Subject 1>) drives continuously forward along the paved road while the woman (<Subject 2>) proudly holds the fluttering red flag in her right hand and energetically waves her left hand, shouting with patriotic excitement. The elevated camera smoothly pans and tracks the vehicle as it drives past beneath the viewpoint and continues toward the gate.

retention_analysis:
<Picture 1> ([Shot 1] first frame): fully_preserved - the exact elevated angle, asphalt lane, surrounding greenery, and oncoming orientation of the black SUV from <Picture 1> strictly establish the opening moment.
<Picture 2> ([Shot 1] last frame): fully_preserved - the rear perspective of the SUV, road markings, escort vehicles, and entrance gate from <Picture 2> strictly define the final landing frame.
<Subject 1> (appears in [Shot 1]): fully_preserved - the car maintains continuous, smooth forward motion along the road surface.
<Subject 2> (appears in [Shot 1]): fully_preserved - the woman remains securely standing in the sunroof, her right hand holding the flag, her left hand waving, and her mouth opening with passionate vocalization.

detailed_description:
The video is a realistic, uplifting documentary-style cinematic shot with natural daylight and smooth elevated camera tracking.
[Shot 1] (Continuous First-to-Last Frame):
- The sequence begins precisely matching <Picture 1>. The black SUV (<Subject 1>) moves steadily forward along the parkway road.
- Standing tall through the panoramic sunroof, the woman (<Subject 2> (S1)) radiates joy and pride. Her right hand firmly holds the flagpole upright; the large red silk flag catches the oncoming airflow and billows with authentic aerodynamic flapping.
- Her left arm is raised high, waving energetically and rhythmically from side to side toward the roadside. Her head tilts slightly upward, her eyes shining with celebration, and she opens her mouth passionately shouting spirited cheers: <d>[Chinese] "国庆快乐！祖国万岁！"</d>, with natural vocal facial expressions and mouth movements.
- As the vehicle drives forward beneath the elevated vantage point, the camera executes a fluid, stable, and continuous pan-and-tilt tracking motion, rotating smoothly to follow the SUV from behind.
- The car glides past the foreground pillar, continuing along the lane behind the accompanying vehicles, perfectly and smoothly aligning into the exact rear composition of <Picture 2> at the final frame.

overall_soundscape:
Realistic outdoor automotive and celebration ambiance: the smooth tire friction and engine purr of the moving SUV, the distinct, energetic flapping and fluttering of the red fabric flag in the breeze, and the woman's spirited, clear cheering voice echoing across the open road.

non_diegetic_music:
An inspiring, majestic, and soaring patriotic orchestral melody featuring uplifting brass fanfares and grand soaring strings, celebrating National Day with warmth, triumph, and vitality.
```

---

## 模板 2：双图联动与文字版式保护（河湾落月、烟花与文字月晕衬底）
* **模式**：海报接 `ref_image_0`（提炼文字排版、落月、烟花、去Logo），航拍接 `ref_image_1`（提供河湾与城市骨架）。
* **特点**：去水印、多行中英文字体景深变焦缩小入场、垫入半透明淡黄色月晕衬底防重叠吞字。

```text
subject_definitions:
<Picture 1> is the first reference image providing the warm golden-amber dusk palette, the colossal glowing celestial moon, the fireworks motif, and the complete central typography assembly. Note: Only the top-left brand logo and the top-right corner logo emblem are strictly removed. All other text elements from <Picture 1> are strictly preserved: the red cursive script "Time Flies", the prominent main title "国庆 拾光 游园会", the subtitle lines "鑫苑·知光集" and "狂欢圆满落幕，美好持续发声", and the bottom spaced golden serif letters "LUMIN TRACE".
<Picture 2> is the second reference image establishing the exact landscape topography: the curved green park peninsula on the left, the tree-lined shoreline on the right, the wide calm river bend in the center, and the modern city skyline across the far bank.
<Subject 1> is the photorealistic urban river bend landscape from <Picture 2>, reimagined under a rich, warm golden-amber sunset dusk, with calm water gently rippling and mirroring the evening sky.
<Subject 2> is the colossal glowing full moon (supermoon) from <Picture 1>, slowly and majestically descending toward the distant city skyline over the river bend, casting a long, shimmering golden reflection across the water.
<Subject 3> is the vibrant, festive fireworks launching and exploding across the twilight sky over the skyline, casting dancing colorful light reflections upon the river's surface.
<Subject 4> is the complete, integrated typography lockup from <Picture 1> ("Time Flies", "国庆 拾光 游园会", "鑫苑·知光集", "狂欢圆满落幕，美好持续发声", "LUMIN TRACE"), entering via a cinematic rack-focus and scale-down animation.
<Subject 5> is the luminous, semi-translucent pale-yellow backing layer situated precisely between the background landscape and the foreground text. It manifests as a soft, ethereal radial moon-halo glow matching the golden-cream color temperature of the moon, blooming into the center to provide a soft-lit, high-contrast backdrop that guarantees razor-sharp legibility for the black and red typography.

summary:
[reference generation + video editing] The target video is a 4.5-to-5-second grand cinematic festive aerial celebration shot combining the full typography and celestial atmosphere of <Picture 1> with the real river bend geography of <Picture 2>. The daytime landscape of <Picture 2> transforms into a warm, magical golden dusk (<Subject 1>). A colossal supermoon (<Subject 2>) slowly descends toward the city skyline over the water, while dazzling fireworks (<Subject 3>) erupt across the sky and reflect in the river. Just before the text appears, a soft, semi-translucent pale-yellow moonlit halo wash (<Subject 5>) blooms in the center. In front of this luminous golden backing, the complete typography assembly (<Subject 4>) smoothly scales down from an out-of-focus blur into pin-sharp focus, creating immaculate contrast and legibility, completely free of any corner logos.

retention_analysis:
<Picture 1> (typography and celestial lighting): partially_preserved - the warm dusk palette, colossal moon, and fireworks are preserved; both top corner brand logos are strictly eliminated; the complete five-part typographic lockup is 100% faithfully preserved in font hierarchy, colors, and layout.
<Picture 2> (landscape topography): fully_preserved - the precise shoreline contours, the left grassy peninsula, the right tree-covered bank, the river bend, and the distant skyline from <Picture 2> strictly form the physical landscape in sunset lighting.
<Subject 1> (appears throughout [Shot 1]): fully_preserved - physical geometry of <Picture 2> with golden-hour illumination and fluid water reflections.
<Subject 2> (appears throughout [Shot 1]): fully_preserved - the giant round moon smoothly sets toward the city skyline.
<Subject 3> (appears in 1.5s-5.0s): attribute_transfer - dynamic pyrotechnic bursts illuminate the sky and water with authentic light bounces.
<Subject 4> (appears in 1.8s-4.5s): attribute_transfer - the entire graphic text layout dynamically scales down from an out-of-focus foreground blur into a sharp, centered title graphic.
<Subject 5> (appears in 1.2s-4.5s): attribute_transfer - blooms softly between the landscape and the text layer as a luminous, translucent pale-yellow backing glow.

detailed_description:
The video is an ultra-high-definition, grand cinematic landscape commercial sequence with authentic atmospheric lighting, multilayered visual depth, and fluid water physics. The upper-left and upper-right corners are completely clean, with zero watermarks or brand logos.
[Shot 1] (0.0s to 4.5s):
- 0.0s - 1.5s: The scene opens with a smooth, high-angle aerial perspective matching the exact framing of <Picture 2>: the green peninsula on the left, the curved shoreline on the right, and the wide calm river bend, all bathed in a warm golden-amber dusk glow. Dominating the sky above the distant urban skyline, a colossal glowing supermoon (<Subject 2>) begins a steady, majestic descent toward the horizon, casting a shimmering ribbon of golden light across the water's surface.
- 1.5s - 3.2s: As the giant moon dips lower toward the skyline, brilliant festive fireworks (<Subject 3>) ignite and explode in the twilight sky. Dazzling showers of gold, crimson, and silver blooms illuminate the clouds and cast vibrant, dancing reflections across the wide water bend, lighting up the tree canopies and green lawns of <Picture 2>.
- 1.2s - 2.0s (Backing Glow Blooming): Just before the text enters, in the central region directly between the river landscape and the camera, a soft, semi-translucent pale-yellow backing glow (<Subject 5>) gently expands and blooms outward. Resembling a radiant, warm moonlit aura or translucent golden light silk, it diffuses smoothly, creating a warm, clean, illuminated atmosphere in the center frame without obscuring the scenery behind it.
- 1.8s - 3.5s (Text Sharp Focusing over Glow): Against this softly illuminated pale-yellow backdrop (<Subject 5>), the complete typography lockup <Subject 4> makes its dramatic entrance. It begins oversized and dreamily blurred (optical bokeh out of focus). In sync with the camera's gentle forward glide, the text smoothly shrinks, scales down toward the center, and steadily shifts into pin-sharp, crisp optical focus:
  - The elegant red script "Time Flies" shines clearly above;
  - The bold title "国庆 拾光 游园会" locks into the center, its black ink characters and vibrant red calligraphic "拾光" strokes contrasting with extreme clarity and crisp definition against the pale-yellow glow;
  - The two Chinese subtitle lines ("鑫苑·知光集" and "狂欢圆满落幕，美好持续发声") crystallize clearly below;
  - The golden spaced letters "LUMIN TRACE" gleam softly across the bottom.
  The entire text group settles into its elegant, balanced poster position with a gentle radiant shimmer by 3.5s.
- 3.5s - 4.5s: The supermoon settles gracefully near the skyline, fireworks continue with celebratory grandeur over the river, and the complete title layout sits crisply, legibly, and beautifully centered on its warm moonlit glow over the tranquil twilight riverbank.

overall_soundscape:
Begins with the serene, gentle breeze blowing over the river and soft water lapping; followed by a subtle, warm luminous whoosh as the golden aura blooms; accompanied by authentic, distant low-frequency thuds and echoing crackles of fireworks erupting across the open water basin, and a delicate, sparkling crystal chime as the full typography snaps into sharp focus.

non_diegetic_music:
A majestic, heartwarming Chinese orchestral composition featuring lyrical Guzheng, soaring bamboo flute, and triumphant festive brass chords, building up an inspiring emotional crescendo that perfectly matches the fireworks and the arrival of the title.
```

---

## 模板 3：一镜到底概念穿越（棉花糖穿云 → 拼豆城市中间帧 → 现实化冲击波 → 真实航拍）
* **模式**：拼豆图接 `ref_image_0`（中间关键帧），真实航拍图接 `ref_image_1`（尾帧）。
* **特点**：严格时间轴分段（0-2.5s / 2.5-3.8s / 3.8-6.5s），虚实材质蜕变与冲击波扩散。

```text
subject_definitions:
<Picture 1> is the intermediate keyframe of [Shot 1] at 2.5s-3.8s, presenting the grand high-angle aerial view of the modern metropolis and construction site constructed entirely out of thousands of colorful 3D cylindrical Perler beads (fuse beads) under bright morning sunlight.
<Picture 2> is the final ending frame of [Shot 1] at 6.5s, presenting the authentic real-world drone aerial photograph of the actual modern skyscrapers, construction site, tower cranes, and green park.
<Subject 1> is the whimsical marshmallow cloudscape that dynamically ruptures into thousands of colorful 3D cylindrical Perler beads with hollow center holes and glossy plastic reflections.
<Subject 2> is the modern metropolis and construction landscape, initially manifested as the fuse-bead architecture of <Picture 1> and transformed into the photorealistic structures of <Picture 2>.
<Subject 3> is the circular reality transition wave (expanding shockwave) detonating at 3.8s from the central towers of <Picture 1> and sweeping outward.

summary:
[keyframe completion + reference generation] The target video is a continuous, single-take 6.5-second cinematic fantasy-to-reality FPV flight. From 0.0s to 2.5s, the camera speeds through giant marshmallow clouds that burst into a dense storm of flying colorful Perler beads (<Subject 1>). From 2.5s to 3.8s, the clouds part widely as the camera smoothly levels out to unveil and match the 3D Perler-bead aerial city of <Picture 1> as the intermediate keyframe. From 3.8s to 6.5s, an expanding luminous reality shockwave (<Subject 3>) detonates from the central towers of <Picture 1>, sweeping outward across the metropolis to transform the plastic beads layer by layer into authentic concrete, glass, steel, and greenery, concluding with a millimeter-precise landing onto <Picture 2>.

retention_analysis:
<Picture 1> ([Shot 1] intermediate keyframe, 2.5s-3.8s): fully_preserved - the exact perspective, framing, building heights, and plastic bead textures of <Picture 1> are faithfully presented as the mid-point reference anchor.
<Picture 2> ([Shot 1] last frame, 6.5s): fully_preserved - the authentic morning daylight, real building materials, road markings, and sky details of <Picture 2> strictly define the final landing frame.
<Subject 1> (appears in 0.0s-2.5s): attribute_transfer - fluffy marshmallow volumes that pop into high-speed plastic beads and disperse.
<Subject 2> (appears in 2.5s-6.5s): attribute_transfer - seamlessly transitions from the fuse-bead structures of <Picture 1> into the photorealistic architecture of <Picture 2>.
<Subject 3> (appears in 3.8s-6.5s): weak_reference - serves as the expanding kinetic transition shockwave detonating from the central towers.

detailed_description:
The video is an ultra-high-definition, seamless single-take FPV drone sequence combining tactile macro physics with epic urban transformation.
[Shot 1] (Continuous 0.0s to 6.5s):
- 0.0s - 2.5s (High-Speed Marshmallow Flight & Bead Burst): The shot opens at blistering forward flight speed through a boundless sea of colossal, fluffy marshmallow clouds (<Subject 1>) with soft, pulling sugar textures and pillowy folds. As the camera cuts at high velocity into the dense cloud core, the marshmallow volumes suddenly pop and disintegrate organically, bursting into thousands of shiny, colorful cylindrical 3D Perler beads (with visible center holes) that whiz violently past the camera lens with intense three-dimensional depth and motion blur.
- 2.5s - 3.8s (Cloud Break & Intermediate Keyframe Reveal): The dense bead storm and sugary mist part dramatically outward to both sides like a grand curtain. Flooded by crisp morning sunlight, the camera smoothly and stably levels out its trajectory, establishing and locking seamlessly into the exact vista of <Picture 1>: the sprawling urban metropolis built entirely of millions of glistening, cylindrical fuse beads, complete with orange plastic cranes, blue curtain walls, and green lawns.
- 3.8s - 6.5s (Center Shockwave & Reality Reconstitution): At exactly 3.8s, a radiant golden pulse detonates from the center towers of <Picture 1>, triggering a sharp, circular reality shockwave (<Subject 3>) that aggressively sweeps outward across the entire frame. Wherever the shockwave passes, the plastic fuse beads instantaneously dissolve and layer-by-layer reconstitute into true physical matter: orange plastic beads transform into real metallic steel cranes, grey beads become raw concrete towers and scaffolding, blue beads turn into reflective glass curtain walls, and green beads blossom into real wind-blown foliage. By 6.5s, the transformation settles completely, locking with millimeter precision into the authentic drone photograph of <Picture 2>.

overall_soundscape:
Begins with a powerful, roaring aerodynamic wind rush layered with the soft "pop" of bursting marshmallows and the frantic clattering of flying plastic beads (0.0s-2.5s); followed by an expansive atmospheric wind release as the clouds part over the bead city (2.5s-3.8s); punctuated at 3.8s by a deep, resonant bass impact boom as the reality shockwave detonates, resolving into the crisp outdoor breeze and authentic city ambience of the real drone flight (3.8s-6.5s).

non_diegetic_music:
A thrilling modern electronic-orchestral hybrid score that starts with high-tempo, playful synth and marimba arpeggios (0.0s-2.5s), swells with driving percussion as the bead city is revealed (2.5s-3.8s), and explodes into an awe-inspiring, epic orchestral fanfare with soaring brass and violins as the world transforms into reality (3.8s-6.5s).
```

---

## 模板 4：高难度人像/肢体防崩坏（美女手部弹钢琴与儿童合影）
* **微距手部（纯文生）**：锁死双手各五指、关节解剖结构、触键下沉回弹、45度微滑轨运镜。
* **群像合影（图生图）**：必须使用 `strictly static locked-off tripod shot` 锁定机位，专注微表情与自然欢笑。
