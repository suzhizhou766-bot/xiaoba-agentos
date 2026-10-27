import {defineSound} from 'origin-audio'
export default defineSound({
 apiVersion:2,duration:0.24,seed:20261022,tail:{mode:'fixed',seconds:0.06},
 output:{gainDb:0,match:{metric:'peak',targetDb:-3,maxBoostDb:30},ceilingDbfs:-1,protection:'attenuate'},
 markers:{cut:0.15},
 setup({dsp,curves}){
  const gate=curves.linear([[0,0],[0.008,.5],[0.035,0],[0.055,0],[0.06,.65],[0.085,0],[0.125,0],[0.15,1],[0.177,0],[0.195,.25],[0.23,0],[0.24,0]])
  const crackle=dsp.noise({type:'white',seed:41,stereo:true}).filter({type:'bandpass',frequency:2100,q:.65}).gain(gate)
  const chirp=dsp.oscillator({type:'sine',frequency:curves.exponential([[0,1900],[.15,700],[.24,420]])}).gain(gate).gain(.12)
  return dsp.mix([crackle,chirp]).gain(.8)
 }
})
