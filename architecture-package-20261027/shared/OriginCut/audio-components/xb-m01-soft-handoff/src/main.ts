import {defineSound} from 'origin-audio'
export default defineSound({
 apiVersion:2,duration:.35,seed:20261022,tail:{mode:'fixed',seconds:.15},
 output:{gainDb:0,match:{metric:'peak',targetDb:-9,maxBoostDb:30},ceilingDbfs:-3,protection:'attenuate'},
 markers:{handoff:.08},parameters:{rise:1},
 setup({dsp,curves,parameters}){
  const rise=parameters.rise===-1?-1:1
  const envelope=curves.linear([[0,0],[.025,.15],[.08,1],[.16,.35],[.3,0],[.35,0]])
  const air=dsp.noise({type:'white',seed:183,stereo:true}).filter({type:'bandpass',frequency:1200,q:.6}).gain(envelope).gain(.19)
  const tone=dsp.oscillator({type:'sine',frequency:curves.exponential([[0,rise>0?330:660],[.08,520],[.3,rise>0?660:330]])}).gain(curves.percussive({attack:.05,decay:.24})).gain(.08)
  return dsp.mix([air,tone])
 }
})
