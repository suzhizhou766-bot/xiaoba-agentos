import{defineSound}from'origin-audio'
export default defineSound({
 apiVersion:2,duration:28,seed:20261023,tail:{mode:'fixed',seconds:2},
 output:{gainDb:0,match:{metric:'peak',targetDb:-16,maxBoostDb:30},ceilingDbfs:-3,protection:'attenuate'},
 markers:{question:0,evidence:4,mechanism:12,conclusion:20,boundary:26},
 setup({dsp,curves}){
  const env=curves.linear([[0,0],[1.8,.32],[4,.25],[10,.18],[12,.3],[18,.2],[20,.32],[25,.18],[26,.3],[28,.15],[29.8,0],[30,0]])
  const root=curves.linear([[0,130.81],[11.9,130.81],[12.1,146.83],[19.9,146.83],[20.1,130.81],[30,130.81]])
  const low=dsp.oscillator({type:'sine',frequency:root}).gain(.35)
  const third=dsp.oscillator({type:'sine',frequency:164.81}).gain(.12)
  const fifth=dsp.oscillator({type:'sine',frequency:196}).gain(.08)
  return dsp.mix([low,third,fifth]).gain(env)
 }
})
