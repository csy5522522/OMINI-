const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
let handler,connection,sent=[],readResult=null,readHook=null;
const tabs={7:{url:'https://omini.xin/#timetable',status:'complete'},8:{url:'https://other.example',status:'complete'},9:{url:'https://sso.hualixy.edu.cn/login',status:'complete'}};
const sender={tab:{id:7},url:tabs[7].url};
const context=vm.createContext({URL,Date,chrome:{
  storage:{session:{get:async()=>({connection}),set:async value=>{connection=value.connection;},remove:async()=>{connection=null;}}},
  tabs:{get:async id=>tabs[id],create:async()=>({id:9}),sendMessage:async(id,message)=>sent.push({id,message}),onUpdated:{addListener:()=>{}}},
  scripting:{executeScript:async()=>[{result:readHook?await readHook():readResult}]},
  cookies:{getAll:async filter=>{assert.equal(filter.url,'https://jw.hualixy.edu.cn/jwglxt/kbcx/xskbcx_cxXsgrkb.html');return [{name:'JSESSIONID',value:'FIXTURE'}];},onChanged:{addListener:()=>{}}},
  runtime:{onMessage:{addListener:fn=>handler=fn}}
}});
vm.runInContext(fs.readFileSync('extras/school-connector/background.js','utf8'),context);
const request=(message,from=sender)=>new Promise(resolve=>handler(message,from,resolve));
const sessions=()=>sent.filter(item=>item.message.type==='school-session');
(async()=>{
  await request({type:'connect',year:'2026',term:'3',requestId:'first'});
  assert.equal(sessions().length,0);assert.equal(connection.loginTabId,9);
  tabs[9].url='https://jw.hualixy.edu.cn/jwglxt/kbcx/index.html';
  readResult={payload:{kbList:[]},year:'2026',term:'3',periods:[]};
  await request({type:'refresh'});
  assert.equal(sessions().length,1);assert.equal(sessions()[0].message.cookie,'JSESSIONID=FIXTURE');assert.equal(sessions()[0].message.automatic,true);
  assert.equal(JSON.stringify(connection).includes('FIXTURE'),false);
  await request({type:'refresh'},{tab:{id:8},url:tabs[8].url});assert.equal(sessions().length,1);
  await request({type:'refresh'},{tab:{id:7},url:tabs[8].url});assert.equal(sessions().length,1);
  await request({type:'refresh'});assert.equal(sessions()[1].message.automatic,false);
  await request({type:'disconnect'});assert.equal(connection,null);
  tabs[9].url='https://sso.hualixy.edu.cn/login';
  await request({type:'connect',year:'2026',term:'3',requestId:'second'});
  tabs[9].url='https://jw.hualixy.edu.cn/jwglxt/kbcx/index.html';
  let release;readHook=()=>new Promise(resolve=>{release=()=>resolve(readResult);});
  const pending=request({type:'refresh'});while(!release)await new Promise(setImmediate);
  const count=sessions().length;await request({type:'disconnect'});release();await pending;assert.equal(sessions().length,count);
  console.log('PASS authenticated-only auto import, no cookie storage, connected tab/origin checks and canceling an in-flight school read');
})().catch(error=>{console.error(error);process.exitCode=1;});
