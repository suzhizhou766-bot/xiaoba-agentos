import {defineSound, type Points} from 'origin-audio'
export default defineSound({
 apiVersion:2,duration:28,seed:1021,tail:{mode:'fixed',seconds:2},
 output:{gainDb:0,match:{metric:'peak',targetDb:-3,maxBoostDb:30},ceilingDbfs:-1,protection:'attenuate'},
 markers:{opening:0,facade:6,garden:10,details:18,reveal:26},
 setup({dsp,curves}){
  const hz=(n:number)=>440*Math.pow(2,(n-69)/12)
  const pulse=(times:number[],width:number,peak:number)=>{
   const points:[number,number][]=[[0,0]]
   for(const at of times){if(at>0)points.push([at,0]);points.push([at+0.003,peak],[at+width*0.22,peak*0.35],[at+width,0])}
   points.push([28,0]);return curves.linear(points as Points)
  }
  const beats=Array.from({length:53},(_,i)=>i*0.5)
  const kickEnv=pulse(beats,0.27,0.46)
  const pitch:[number,number][]=[]
  for(const at of beats)pitch.push([at,145],[at+0.035,62],[at+0.27,43],[at+0.499,43])
  const kick=dsp.oscillator({type:'sine',frequency:curves.exponential(pitch)}).gain(kickEnv)
  const snare=dsp.noise({type:'white',seed:321}).filter({type:'bandpass',frequency:1900,q:0.7})
    .gain(pulse(Array.from({length:26},(_,i)=>i+0.5),0.13,0.12))
  const hat=dsp.noise({type:'white',seed:87}).filter({type:'highpass',frequency:7500,q:0.7})
    .gain(pulse(Array.from({length:82},(_,i)=>6+i*0.25),0.045,0.04))
  const chords=[[50,57,65],[46,53,62],[48,57,65],[48,55,64],[50,57,65],[46,53,62],[50,57,65]]
  const voices=[]
  for(let voice=0;voice<3;voice++){
   const freq:[number,number][]=[]
   chords.forEach((c,i)=>{if(i)freq.push([i*4-0.002,hz(chords[i-1][voice])]);freq.push([i*4,hz(c[voice])])})
   voices.push(dsp.oscillator({type:'triangle',frequency:curves.linear(freq)})
    .filter({type:'lowpass',frequency:1500,q:0.6})
    .gain(pulse([0,4,8,12,16,20,24],3.95,0.07)).pan((voice-1)*0.4))
  }
  const bassFreq:[number,number][]=[]
  chords.forEach((c,i)=>{if(i)bassFreq.push([i*4-0.002,hz(chords[i-1][0]-12)]);bassFreq.push([i*4,hz(c[0]-12)])})
  const bass=dsp.oscillator({type:'triangle',frequency:curves.linear(bassFreq)}).filter({type:'lowpass',frequency:400,q:0.7})
    .gain(pulse(Array.from({length:45},(_,i)=>4+i*0.5),0.38,0.15))
  const melodyFreq:[number,number][]=[]
  const melodyTimes:number[]=[]
  for(let i=0;i<18;i++){
   const at=8+i,mid=chords[Math.floor(at/4)][i%3]+12
   if(i)melodyFreq.push([at-0.002,melodyFreq[melodyFreq.length-1][1]])
   melodyFreq.push([at,hz(mid)]);melodyTimes.push(at+0.25)
  }
  const melody=dsp.fm({frequency:curves.linear(melodyFreq),ratio:2,index:0.45})
    .gain(pulse(melodyTimes,0.7,0.06)).delay({time:0.375,feedback:0.16,mix:0.15,maxTime:0.5}).pan(0.2)
  const airPts:[number,number][]=[[0,0]]
  for(const at of [9.4,17.4,25.4])airPts.push([at,0],[at+0.45,0.045],[at+0.6,0])
  airPts.push([28,0])
  const air=dsp.noise({type:'pink',seed:251,stereo:true}).filter({type:'bandpass',frequency:2200,q:0.6}).gain(curves.smooth(airPts))
  const endChord=dsp.mix([62,65,69].map(n=>dsp.oscillator({type:'triangle',frequency:hz(n)}).gain(0.042)))
    .filter({type:'lowpass',frequency:1800,q:0.6})
    .gain(curves.linear([[0,0],[25.999,0],[26,1],[27.98,1],[28,0]]))
  const hit=dsp.resonator({frequency:hz(62),decay:1.6,partials:[1,0.4,0.2],seed:77}).gain(0.18).at(26)
  return dsp.mix([kick,snare,hat,...voices,bass,melody,air,hit,endChord])
    .gain(curves.smooth([[0,0],[0.1,1],[27.94,1],[27.999,0]]))
    .reverb({decay:3.5,damping:0.6,mix:0.32})
 }
})
