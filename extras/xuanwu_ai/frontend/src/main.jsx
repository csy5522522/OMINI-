import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { QiviAvatar, QiviVoice, CHARACTERS } from 'qivi-react';
import 'qivi-react/styles.css';
import { Plus, MessageSquare, Settings, ArrowUp, Square, RefreshCw, Cpu, X, Trash2, Copy, Check, Volume2, Sparkles, ChevronDown, Shield, Download, Sun, Moon, PanelLeftClose, PanelLeftOpen, Paperclip, Mic, MicOff, FolderOpen, FileText, Image as ImageIcon, Wrench, ArrowDown, Terminal, Plug, Users, CircleCheck, CircleX, RotateCw, Bot, BrainCircuit } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import './style.css';

const uid = () => crypto.randomUUID();
const characterNames = {qivi: 'Qivi · 经典', female: 'Nova · 柔和', male: 'Sol · 沉稳', ember: 'Ember · 星火', sage: 'Sage · 薄荷', atlas: 'Atlas · 守护'};
const stateNames = {idle: '等待你的下一句话', listening: '我在听，继续说', thinking: '正在思考你的问题', answering: '正在为你组织回答', talking: '正在朗读回答', analyzing: '正在执行本机操作', error: '连接遇到了一点问题'};
const starterAgents = [
  {id:'xuanwu',name:'玄武',character:'qivi',system:'你是一位友好、准确的中文本地智能助手。优先使用中文回答。'},
  {id:'engineer',name:'工程师',character:'atlas',system:'你擅长编程、终端命令和排查问题。先检查实际文件与命令结果，再给出结论。'},
  {id:'planner',name:'规划师',character:'sage',system:'你擅长把复杂目标拆成清晰、可执行的步骤，并说明关键取舍。'},
  {id:'researcher',name:'分析师',character:'ember',system:'你擅长研究、比较选项、整理证据与发现遗漏。'},
];
const permissionModes = [
  {id:'approve',name:'请求批准',description:'每次调用本机文件、终端或 MCP 工具前都请你确认。'},
  {id:'smart',name:'帮我批准',description:'只读操作自动执行；写入、删除、命令和未标注只读的 MCP 操作会询问。'},
  {id:'full',name:'完全访问权限',description:'直接访问电脑上的文件、命令行及已启用 MCP 服务，不逐项弹出确认。'},
];
const newSession = (agentId='xuanwu') => ({id: uid(), agent_id:agentId, title: '新的对话', messages: [], updated: Date.now()/1000});

function App() {
  const [settings, setSettings] = useState({endpoint:'http://127.0.0.1:11434', model:'', quality:'medium', character:'qivi', voice:false, system:'', theme:'dark', sidebar_collapsed:false, tools_enabled:true, shell_enabled:true, workspace:'', agents:starterAgents, active_agent_id:'xuanwu', permission_mode:'approve', skills_enabled:[], mcp_servers:[]});
  const [models, setModels] = useState([]);
  const [connected, setConnected] = useState(false);
  const [checking, setChecking] = useState(false);
  const [sessions, setSessions] = useState([]);
  const [current, setCurrent] = useState(newSession);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [phase, setPhase] = useState('idle');
  const [error, setError] = useState('');
  const [ready, setReady] = useState(false);
  const [modal, setModal] = useState(false);
  const [agentEditorId, setAgentEditorId] = useState('xuanwu');
  const [availableSkills, setAvailableSkills] = useState([]);
  const [mcpStatus, setMcpStatus] = useState([]);
  const [mcpDraft, setMcpDraft] = useState({name:'',transport:'stdio',command:'npx',args:'[]',cwd:'',env:'{}',url:'',headers:'{}'});
  const [pendingApproval, setPendingApproval] = useState(null);
  const [approvalBusy, setApprovalBusy] = useState(false);
  const [draft, setDraft] = useState(null);
  const [saving, setSaving] = useState(false);
  const [voice, setVoice] = useState(null);
  const [speaking, setSpeaking] = useState(false);
  const [copied, setCopied] = useState('');
  const [attachments, setAttachments] = useState([]);
  const [uploading, setUploading] = useState(false);
  const [recording, setRecording] = useState(false);
  const [dictation, setDictation] = useState('');
  const [speechPending,setSpeechPending]=useState(false);
  const dictationRun=useRef(0);
  const [scrolledUp, setScrolledUp] = useState(false);
  const fileInput = useRef(null);
  const messagesBox = useRef(null);
  const followBottom = useRef(true);
  const speechPoll = useRef(null);
  const dictationBase = useRef('');
  const token = useRef('');
  const generation = useRef(null);
  const speech = useRef(null);
  const avatar = useRef(null);
  const bottom = useRef(null);
  const textarea = useRef(null);
  const manualVoice = useMemo(() => QiviVoice.manual(), []);
  const agents = settings.agents?.length ? settings.agents : starterAgents;
  const activeAgent = agents.find(a=>a.id===current.agent_id) || agents.find(a=>a.id===settings.active_agent_id) || agents[0];
  const draftAgents = draft?.agents?.length ? draft.agents : starterAgents;
  const editingAgent = draftAgents.find(a=>a.id===agentEditorId) || draftAgents[0];
  const activeState = speaking ? 'talking' : recording ? 'listening' : phase === 'idle' && input.trim() ? 'listening' : phase;

  async function api(path, body) {
    const response = await fetch('/api/' + path, {method: body === undefined ? 'GET' : 'POST', headers: {'Content-Type':'application/json', 'X-Xuanwu-Token':token.current}, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '请求失败');
    return result;
  }
  async function refreshIntegrations() {
    try {const result=await api('skills');setAvailableSkills(result.skills||[]);}catch(e){setAvailableSkills([]);}
    try {const result=await api('mcp');setMcpStatus(result.servers||[]);}catch(e){setMcpStatus([]);}
  }
  function openSettings() {
    const copy=JSON.parse(JSON.stringify(settings));
    setDraft(copy);setAgentEditorId(activeAgent.id);setModal(true);refreshIntegrations();
  }
  function updateDraftAgent(field,value) {
    if(!draft||!editingAgent)return;
    setDraft({...draft,agents:draftAgents.map(agent=>agent.id===editingAgent.id?{...agent,[field]:value}:agent)});
  }
  function addAgent() {
    const used=new Set(draftAgents.map(agent=>agent.character));
    const character=Object.keys(characterNames).find(key=>!used.has(key));
    if(!character){setError('最多配置 6 个 Agent；每个 Agent 使用一种不同的 Qivi 粒子角色。');return;}
    const agent={id:'agent_'+uid().replaceAll('-','').slice(0,10),name:'新 Agent',character,system:'你是一个专注的协作 Agent。结合自己的专长，给出准确、可执行的答复。'};
    setDraft({...draft,agents:[...draftAgents,agent]});setAgentEditorId(agent.id);
  }
  function removeAgent() {
    if(draftAgents.length<=1||!editingAgent)return;
    const next=draftAgents.filter(agent=>agent.id!==editingAgent.id);
    setDraft({...draft,agents:next,active_agent_id:draft.active_agent_id===editingAgent.id?next[0].id:draft.active_agent_id});
    setAgentEditorId(next[0].id);
  }
  function addMcpServer() {
    if(!draft||!mcpDraft.name.trim()){setError('请先填写 MCP 服务名称。');return;}
    try {
      const record={name:mcpDraft.name.trim(),transport:mcpDraft.transport,enabled:true};
      if(record.transport==='stdio'){
        record.command=mcpDraft.command.trim();record.args=JSON.parse(mcpDraft.args||'[]');record.cwd=mcpDraft.cwd.trim();record.env=JSON.parse(mcpDraft.env||'{}');
      }else{
        record.url=mcpDraft.url.trim();record.headers=JSON.parse(mcpDraft.headers||'{}');
      }
      const existing=(draft.mcp_servers||[]).filter(server=>server.name!==record.name);
      setDraft({...draft,mcp_servers:[...existing,record]});setMcpDraft({...mcpDraft,name:''});
    }catch(e){setError('MCP 参数需要是有效 JSON：'+e.message);}
  }
  function switchAgent(id) {
    if(busy||recording||speechPending||id===activeAgent.id)return;
    silence();setCurrent(newSession(id));setInput('');setAttachments([]);setError('');setPendingApproval(null);
    const next={...settings,active_agent_id:id};setSettings(next);
    api('settings',next).then(setSettings).catch(e=>setError(e.message));
  }
  async function refreshModels(config = settings) {
    setChecking(true);
    try {
      const result = await api('models');
      setModels(result.models); setConnected(true); setError('');
      if (result.models.length && !result.models.some(m => m.name === config.model)) {
        const updated = {...config, model: result.models[0].name};
        const saved = await api('settings', updated); setSettings(saved);
      }
      if (!result.models.length) setError('Ollama 已连接，尚未发现本地模型。请先在 Ollama 下载模型，再点击重新连接。');
    } catch (e) {setConnected(false); setError(e.message);} finally {setChecking(false);}
  }
  useEffect(() => {
    fetch('/api/bootstrap').then(r => {if (!r.ok) throw new Error('本地服务启动失败'); return r.json();}).then(data => {
      token.current = data.token;
      const restoredSettings={...data.settings,agents:data.settings.agents?.length?data.settings.agents:starterAgents,
        active_agent_id:data.settings.active_agent_id||'xuanwu',permission_mode:data.settings.permission_mode||'approve',
        shell_enabled:data.settings.shell_enabled??true,skills_enabled:data.settings.skills_enabled||[],mcp_servers:data.settings.mcp_servers||[]};
      const restoredSessions=(data.sessions||[]).map(session=>({...session,agent_id:session.agent_id||restoredSettings.active_agent_id}));
      setSettings(restoredSettings); setSessions(restoredSessions);
      if (restoredSessions.length) setCurrent(restoredSessions[0]); else setCurrent(newSession(restoredSettings.active_agent_id));
      setReady(true); refreshModels(data.settings);
    }).catch(e => setError(e.message));
    return () => {clearInterval(speechPoll.current);generation.current?.controller.abort(); speech.current?.dispose(); manualVoice.dispose();};
  }, []);
  useEffect(() => {if (followBottom.current && messagesBox.current) messagesBox.current.scrollTop=messagesBox.current.scrollHeight;}, [current.messages, busy]);
  useEffect(() => {followBottom.current=true;setScrolledUp(false);if(messagesBox.current)messagesBox.current.scrollTop=messagesBox.current.scrollHeight;},[current.id]);
  useEffect(() => {document.documentElement.dataset.theme=settings.theme||'dark';},[settings.theme]);
  useEffect(() => {if (textarea.current) {textarea.current.style.height = 'auto'; textarea.current.style.height = Math.min(textarea.current.scrollHeight, 144) + 'px';}}, [input]);

  async function persist(session) {
    setSessions(old => [session, ...old.filter(s => s.id !== session.id)].slice(0,100));
    try {await api('session', session);} catch (e) {setError('聊天记录保存失败：' + e.message);}
  }
  function silence() {speech.current?.dispose(); speech.current = null; setVoice(null); setSpeaking(false);}
  async function speak(text) {
    silence();
    if (!('speechSynthesis' in window) || !speechSynthesis.getVoices().length) {setError('系统暂无可用语音。可在 Windows 添加中文语音后重新打开程序。'); return;}
    const plain = text.replace(/```[\s\S]*?```/g, '，代码内容请查看屏幕。').replace(/[#*_`>]/g, '').slice(0,6000);
    const selected = speechSynthesis.getVoices().find(v => v.lang.startsWith('zh') && v.localService) || speechSynthesis.getVoices().find(v => v.lang.startsWith('zh'));
    const run = QiviVoice.speak(plain, {lang:'zh-CN', voice:selected, rate:1});
    speech.current = run.voice; setVoice(run.voice); setSpeaking(true);
    await run.done;
    if (speech.current === run.voice) {run.voice.dispose(); speech.current=null; setVoice(null); setSpeaking(false);}
  }
  async function send(text = input) {
    const value = text.trim() || (attachments.length?'请分析这些附件。':''); if (!value || busy || uploading || recording || speechPending || !ready || !connected || !settings.model) return;
    silence(); setError(''); setInput(''); setBusy(true); setPhase('thinking');
    const userMessage = {id:uid(), role:'user', content:value, attachments:[...attachments]};setAttachments([]);followBottom.current=true;setScrolledUp(false);
    const answer = {id:uid(), role:'assistant', content:'', thinking:'', tools:[], agents:[], agent_id:activeAgent.id, agent_name:activeAgent.name, character:activeAgent.character, model:settings.model};
    const before = [...current.messages, userMessage];
    let session = {...current, title: current.messages.length ? current.title : value.slice(0,22), messages:[...before, answer]};
    const request = {id:uid(), controller:new AbortController(), stopped:false}; generation.current=request;
    setCurrent(session);
    try {
      await persist(session);
      if (request.stopped) return;
      const response = await fetch('/api/chat', {method:'POST', headers:{'Content-Type':'application/json','X-Xuanwu-Token':token.current}, signal:request.controller.signal, body:JSON.stringify({id:request.id, model:settings.model, agent_id:activeAgent.id, messages:before.filter(m => m.content||m.attachments?.length||m.tools?.length).map(m => ({role:m.role,content:(m.content||'')+(m.tools?.length?'\n\n[已执行工具记录]\n'+JSON.stringify(m.tools.filter(t=>t.result).map(t=>({name:t.name,arguments:t.arguments,result:t.result}))).slice(0,40000):''),attachments:(m.attachments||[]).map(a=>a.id)}))})});
      if (!response.ok) {const data=await response.json(); throw new Error(data.error || '生成失败');}
      const reader=response.body.getReader(); const decoder=new TextDecoder(); let buffer=''; let finished=false;
      function consume(line) {
        if (!line.trim()) return;
        const event=JSON.parse(line);
        if (event.type==='content') {answer.content+=event.text; setPhase('answering'); manualVoice.pulse(0.35);}
        if (event.type==='thinking') {answer.thinking+=event.text; setPhase('thinking');}
        if (event.type==='tool') {answer.tools=answer.tools.filter(t=>t.id!==event.id).concat(event);setPhase('analyzing');}
        if (event.type==='agent') {answer.agents=(answer.agents||[]).filter(a=>a.id!==event.id).concat(event);setPhase('analyzing');}
        if (event.type==='approval_required') {setPendingApproval(event);setPhase('analyzing');}
        if (event.type==='done') {answer.tokens=event.tokens; answer.speed=event.speed; finished=true;}
        if (event.type==='stopped') {request.stopped=true; finished=true;}
        if (event.type==='error') throw new Error(event.text);
        session={...session, messages:[...before,{...answer}]}; setCurrent(session);
      }
      while (true) {
        const chunk=await reader.read(); if (chunk.done) break;
        buffer+=decoder.decode(chunk.value,{stream:true});
        let boundary; while ((boundary=buffer.indexOf('\n'))>=0) {const line=buffer.slice(0,boundary); buffer=buffer.slice(boundary+1); consume(line);}
      }
      buffer+=decoder.decode(); if (buffer.trim()) consume(buffer);
      if (!finished && !request.stopped) throw new Error('生成连接提前结束，请重试。');
      if (settings.voice && answer.content && !request.stopped) speak(answer.content);
    } catch (e) {
      if (e.name!=='AbortError') {answer.error=e.message; setError(e.message);}
    } finally {
      answer.stopped=request.stopped;
      session={...session, messages:[...before,{...answer}]}; setCurrent(session);
      await persist(session); generation.current=null; setBusy(false); setPhase('idle');
      textarea.current?.focus();
    }
  }
  async function stop() {
    const request=generation.current; if (!request) return;
    request.stopped=true;
    try {await api('stop',{id:request.id});} catch (e) {setError(e.message);}
    setPendingApproval(null);
    request.controller.abort();
  }
  async function decideApproval(approved) {
    if(!pendingApproval||approvalBusy)return;
    setApprovalBusy(true);
    try {await api('approval',{id:pendingApproval.request_id,approval_id:pendingApproval.id,approved});setPendingApproval(null);}
    catch(e){setError(e.message);}finally{setApprovalBusy(false);}
  }
  function startNew() {if (busy||recording||speechPending) return; silence(); setCurrent(newSession(activeAgent.id)); setInput('');setAttachments([]); setError('');setPendingApproval(null); textarea.current?.focus();}
  async function remove(session) {
    try {await api('delete',{id:session.id}); setSessions(old=>old.filter(s=>s.id!==session.id)); if (current.id===session.id) startNew();} catch (e) {setError(e.message);}
  }
  async function saveSettings(e) {
    e.preventDefault(); setSaving(true);
    try {const saved=await api('settings',draft); setSettings(saved);if(!saved.agents.some(agent=>agent.id===current.agent_id))setCurrent(newSession(saved.active_agent_id));setModal(false); await refreshModels(saved);refreshIntegrations();} catch (e) {setError(e.message);} finally {setSaving(false);}
  }
  async function copy(message) {try {await navigator.clipboard.writeText(message.content); setCopied(message.id); setTimeout(()=>setCopied(''),1400);} catch {setError('复制未完成，请选中文字后按 Ctrl+C。');}}
  function exportChat() {
    const text=`# ${current.title}\n\n`+current.messages.map(m=>`## ${m.role==='user'?'你':'玄武Ai'}\n\n${m.content}\n`+(m.attachments?.length?'\n附件：'+m.attachments.map(a=>a.name).join('、')+'\n':'')+(m.tools?.length?'\n工具记录：\n'+m.tools.map(t=>`- ${t.name} (${t.status}): ${JSON.stringify(t.result||{})}`).join('\n')+'\n':'')).join('\n');
    const url=URL.createObjectURL(new Blob([text],{type:'text/markdown;charset=utf-8'})); const link=document.createElement('a'); link.href=url; link.download='玄武Ai-对话.md'; link.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  const toolNames={list_directory:'列出目录',read_file:'读取文件',search_files:'搜索文件',write_file:'写入文件',create_directory:'创建文件夹',move_path:'移动 / 重命名',trash_file:'移入备份区',run_command:'执行命令'};
  async function updatePreferences(values) {
    const next={...settings,...values};setSettings(next);
    try {const saved=await api('settings',next);setSettings(saved);}catch(e){setSettings(settings);setError(e.message);}
  }
  async function chooseWorkspace() {
    try {const result=await api('pick-folder',{});if(result.path)await updatePreferences({workspace:result.path});else if(result.manual)openSettings();}catch(e){setError(e.message);}
  }
  async function uploadFiles(e) {
    const files=Array.from(e.target.files||[]);e.target.value='';
    if(files.length+attachments.length>8){setError('每条消息最多 8 个附件');return;}
    setUploading(true);setError('');
    try {for(const file of files){
      if(file.size>10_000_000)throw new Error(file.name+' 超过 10 MB');
      const encoded=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=()=>reject(new Error('读取附件失败'));reader.readAsDataURL(file);});
      const result=await api('attachment',{name:file.name,data:encoded});setAttachments(old=>[...old,result]);
    }}catch(e){setError(e.message);}finally{setUploading(false);}
  }
  async function finishDictation() {
    dictationRun.current++;clearInterval(speechPoll.current);speechPoll.current=null;setSpeechPending(true);
    try{const result=await api('speech/stop',{});setInput(dictationBase.current+(result.text||''));if(result.error)setError(result.error);}catch(e){setError(e.message);}finally{setRecording(false);setSpeechPending(false);setDictation('');textarea.current?.focus();}
  }
  async function toggleDictation() {
    if(speechPending)return;if(recording){await finishDictation();return;}
    silence();setError('');dictationBase.current=input;setSpeechPending(true);const run=++dictationRun.current;
    try {await api('speech/start',{});setRecording(true);setDictation('');
      speechPoll.current=setInterval(async()=>{try{const result=await api('speech/status');if(run!==dictationRun.current)return;setDictation(result.partial);setInput(dictationBase.current+(result.text||''));if(!result.active){clearInterval(speechPoll.current);speechPoll.current=null;setRecording(false);setDictation('');if(result.error)setError(result.error);}}catch(e){clearInterval(speechPoll.current);setRecording(false);setError(e.message);}},500);
    }catch(e){setError(e.message);}finally{setSpeechPending(false);}
  }
  const lastMessage=current.messages[current.messages.length-1];
  const avatarProps = {ref:avatar, size:'100%', character:CHARACTERS[activeAgent.character], personality:'guardian', appearance:settings.theme==='light'?'light':'dark', theme:'arctic', quality:settings.quality, state:activeState, voice:speaking?voice:activeState==='answering'?manualVoice:null, bleed:0.35, interactive:true, lighting:0.9, intensity:0.7, autoSleep:false, label:activeAgent.name+' 粒子角色', onPoke:()=>avatar.current?.impulse('ripple')};

  return <div className={'app-shell '+(settings.sidebar_collapsed?'sidebar-collapsed':'')}>
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark"><img src="./omini-logo.png" alt="Omini"/></div><span>玄武<span className="cyan">Ai</span></span></div>
      <button className="new-chat" onClick={startNew} disabled={busy||recording||speechPending}><Plus size={17}/>新建对话<span>＋</span></button>
      <div className="nav-active"><MessageSquare size={17}/>对话空间</div>
      <div className="section-label agent-section-label">AGENTS <span>{agents.length}</span></div>
      <div className="agent-list">{agents.map(agent=><button key={agent.id} className={'agent-row '+(activeAgent.id===agent.id?'selected':'')} disabled={busy||recording||speechPending} onClick={()=>switchAgent(agent.id)} title={`${agent.name} · ${characterNames[agent.character]}`}><span className="agent-portrait"><QiviAvatar size="32px" character={CHARACTERS[agent.character]} personality="guardian" appearance={settings.theme==='light'?'light':'dark'} theme="arctic" quality="low" state="idle" interactive={false} lighting={0.8} intensity={0.6} autoSleep={false} label={`${agent.name} 粒子角色`}/></span><span className="agent-row-copy"><strong>{agent.name}</strong><small>{characterNames[agent.character]}</small></span>{activeAgent.id===agent.id&&<span className="agent-active-dot"/>}</button>)}</div>
      <div className="section-label">最近对话 <span>{sessions.length.toString().padStart(2,'0')}</span></div>
      <div className="history">{sessions.length ? sessions.map(s=><div key={s.id} className={'history-row '+(current.id===s.id?'selected':'')}>
        <button disabled={busy||recording||speechPending} onClick={()=>{silence();setCurrent(s);setAttachments([]);setError('');}}><MessageSquare size={14}/><span>{s.title}</span></button>
        <button disabled={busy||recording||speechPending} className="delete icon-button" title="删除对话" aria-label={'删除 '+s.title} onClick={()=>remove(s)}><Trash2 size={13}/></button>
      </div>) : <div className="history-empty">你的想法，将从这里开始。</div>}</div>
      <div className="sidebar-bottom"><div className="local-badge"><Shield size={15}/><div>本地对话<span>记录保存在这台电脑</span></div></div><button className="settings-button" disabled={busy||recording||speechPending} onClick={openSettings}><Settings size={17}/>设置与连接</button><div className="version">XUANWU AI <span>v1.2</span></div></div>
    </aside>
    <main className="workspace">
      <header className="topbar"><div className="topbar-start"><button className="icon-button" aria-label={settings.sidebar_collapsed?'展开侧边栏':'收起侧边栏'} title={settings.sidebar_collapsed?'展开侧边栏':'收起侧边栏'} disabled={!ready} onClick={()=>updatePreferences({sidebar_collapsed:!settings.sidebar_collapsed})}>{settings.sidebar_collapsed?<PanelLeftOpen size={19}/>:<PanelLeftClose size={19}/>}</button><div className="workspace-label"><span className="little-dot"/>PERSONAL INTELLIGENCE</div></div><div className="topbar-actions"><button className="icon-button" aria-label={settings.theme==='light'?'切换黑夜模式':'切换白天模式'} title={settings.theme==='light'?'切换黑夜模式':'切换白天模式'} disabled={!ready} onClick={()=>updatePreferences({theme:settings.theme==='light'?'dark':'light'})}>{settings.theme==='light'?<Moon size={18}/>:<Sun size={18}/>}</button><button className="icon-button mobile-settings" aria-label="打开设置" disabled={busy||recording||speechPending} onClick={openSettings}><Settings size={18}/></button><div className="connection"><span className={connected?'status-dot online':'status-dot'}/>{checking?'正在连接':connected?'Ollama 已连接':'Ollama 未连接'}<button className={'icon-button '+(checking?'spin':'')} disabled={checking||busy||!ready} onClick={()=>refreshModels()} aria-label="重新连接" title="重新连接"><RefreshCw size={15}/></button></div></div></header>
      <div className="workspace-body">
        <section className="hero">
          <div className="hero-heading"><span className="eyebrow">YOUR LOCAL COMPANION · {activeAgent.name.toUpperCase()}</span><h1>玄武<span>Ai</span><i> / </i>对话，拥有形态。</h1><p>让每一个想法，都得到回应。</p></div>
          <div className={'avatar-scene phase-'+activeState}><div className="orbit orbit-one"/><div className="orbit orbit-two"/><div className="crosshair c-one"/><div className="crosshair c-two"/><div className="avatar-container"><QiviAvatar {...avatarProps}/></div><span className="scene-label">XW · CORE</span></div>
          <div className="avatar-status" aria-live="polite"><div className={'equalizer '+(['thinking','answering','talking','analyzing'].includes(activeState)?'moving':'')}>{Array.from({length:13},(_,i)=><i key={i} style={{'--bar':Math.abs(6-i),'--delay':`${i*.075}s`}}/>)}</div><p>{activeAgent.name} · {stateNames[activeState] || stateNames.idle}</p><span>QIVI PARTICLE ENGINE · {settings.quality==='high'?'38,000':settings.quality==='low'?'6,000':'16,000'} POINTS</span></div>
          <div className="hero-footer"><span><Cpu size={13}/> 本机模型推理</span><span><Sparkles size={13}/> 动态粒子角色</span></div>
        </section>
        <section className="chat-panel">
          <div className="chat-toolbar"><div><MessageSquare size={17}/><span>对话</span></div><div className="chat-toolbar-controls"><div className="agent-picker"><Users size={14}/><select value={activeAgent.id} disabled={busy||agents.length<2} aria-label="选择 Agent" onChange={e=>switchAgent(e.target.value)}>{agents.map(agent=><option key={agent.id} value={agent.id}>{agent.name} · {characterNames[agent.character]}</option>)}</select><ChevronDown size={12}/></div><div className="model-picker"><Cpu size={14}/><select value={settings.model} disabled={busy||!models.length} aria-label="选择本地模型" onChange={async e=>{const updated={...settings,model:e.target.value};setSettings(updated);try{await api('settings',updated);}catch(e){setError(e.message);}}}>{!models.length&&<option value="">暂无模型</option>}{models.map(m=><option key={m.name} value={m.name}>{m.name}</option>)}</select><ChevronDown size={13}/></div></div></div>
          <div className="workspace-strip"><button disabled={busy} onClick={chooseWorkspace} title={settings.workspace}><FolderOpen size={13}/><span>{settings.workspace?.split(/[\\/]/).filter(Boolean).pop()||'选择工作目录'}</span></button><span className={settings.tools_enabled||settings.shell_enabled?'tools-on':''}><Wrench size={12}/>{settings.tools_enabled?'文件工具':'文件工具已关闭'} · {settings.shell_enabled?'命令行已启用':'命令行已关闭'}</span><span className={'mode-badge mode-'+settings.permission_mode}>{permissionModes.find(mode=>mode.id===settings.permission_mode)?.name}</span></div>
          <div className="messages-wrapper"><div className="messages" ref={messagesBox} onScroll={e=>{const box=e.currentTarget;const near=box.scrollHeight-box.scrollTop-box.clientHeight<60;followBottom.current=near;setScrolledUp(!near);}} role="log" aria-label="聊天记录" tabIndex={0}>
            {!current.messages.length && <div className="welcome"><div className="welcome-icon"><img src="./omini-logo.png" alt="Omini"/></div><h2>你好，我是{activeAgent.name}。</h2><p>连接本地模型，开始一段新的对话。</p><div className="suggestions">{['帮我梳理今天的计划','解释一个有趣的科学概念','帮我写一个 Python 小工具'].map(text=><button key={text} disabled={!connected||!settings.model} onClick={()=>send(text)}>{text}<ArrowUp size={14}/></button>)}</div></div>}
            {current.messages.map(message=><article key={message.id} className={'message '+message.role}><div className="message-heading"><span className={'message-avatar '+message.role}>{message.role==='user'?'你':<img src="./omini-logo.png" alt="Omini"/>}</span><span>{message.role==='user'?'你':(message.agent_name||'玄武Ai')}</span>{message.model&&<small>{message.model}</small>}</div><div className="message-body">
              {message.attachments?.length>0&&<div className="message-attachments">{message.attachments.map(a=><span key={a.id}>{a.kind==='image'?<ImageIcon size={13}/>:<FileText size={13}/>} {a.name}</span>)}</div>}
              {message.agents?.map(run=><div className={'agent-run '+(run.status||'')} key={run.id}><span className="agent-run-avatar"><QiviAvatar size="38px" character={CHARACTERS[run.character||'qivi']} personality="guardian" appearance={settings.theme==='light'?'light':'dark'} theme="arctic" quality="low" state={run.status==='running'?'thinking':'idle'} interactive={false} lighting={0.8} intensity={0.6} autoSleep={false} label={`${run.name} 协作角色`}/></span><span><strong>{run.name||'协作 Agent'}</strong><small>{run.status==='running'?'正在独立分析':run.status==='error'?'协作出错':'已返回分析结果'}</small></span><BrainCircuit size={14}/></div>)}
              {message.tools?.map(tool=><details className={'tool-call '+tool.status} key={tool.id}><summary><Wrench size={13}/><span>{toolNames[tool.name]||tool.name}</span><small>{tool.status==='running'?'执行中':tool.status==='error'?'执行出错':'已执行'}</small></summary><pre>{JSON.stringify({参数:tool.arguments,结果:tool.result},null,2)}</pre></details>)}
              {message.thinking && <details className="thinking"><summary>思考过程</summary><div>{message.thinking}</div></details>}
              {message.content ? <ReactMarkdown remarkPlugins={[remarkGfm]} components={{a:props=><a {...props} target="_blank" rel="noreferrer"/>}}>{message.content}</ReactMarkdown> : !message.error&&!message.stopped&&!message.tools?.length&&<div className="typing"><i/><i/><i/><span>{busy?'正在思考…':'等待回复'}</span></div>}
              {message.error&&<p className="message-error">{message.error}</p>}{message.stopped&&<small className="stopped">已停止生成</small>}
              {message.role==='assistant'&&message.content&&<div className="message-actions"><button className="icon-button" title="复制" aria-label="复制回答" onClick={()=>copy(message)}>{copied===message.id?<Check size={14}/>:<Copy size={14}/>}</button><button className="icon-button" title="朗读" aria-label="朗读回答" onClick={()=>speak(message.content)}><Volume2 size={14}/></button>{message.speed!=null&&<span>{message.speed} token/s</span>}</div>}
            </div></article>)}<div ref={bottom}/>
          </div>{scrolledUp&&<button className="jump-bottom" onClick={()=>{followBottom.current=true;setScrolledUp(false);messagesBox.current.scrollTo({top:messagesBox.current.scrollHeight,behavior:'smooth'});}}><ArrowDown size={14}/>回到最新</button>}</div>
          {pendingApproval&&<div className="approval-card"><div className="approval-heading"><span><Shield size={16}/> 操作等待批准</span><small>{permissionModes.find(mode=>mode.id===pendingApproval.mode)?.name}</small></div><p><strong>{toolNames[pendingApproval.name]||pendingApproval.name}</strong> 将使用以下参数：</p><pre>{JSON.stringify(pendingApproval.arguments,null,2)}</pre><div className="approval-actions"><button className="approval-deny" disabled={approvalBusy} onClick={()=>decideApproval(false)}><CircleX size={15}/>拒绝这次</button><button className="approval-allow" disabled={approvalBusy} onClick={()=>decideApproval(true)}><CircleCheck size={15}/>{approvalBusy?'正在提交…':'允许这次操作'}</button></div></div>}
          {error&&<div className="error-banner" role="alert"><span>{error}</span><button className="icon-button" aria-label="关闭提示" onClick={()=>setError('')}><X size={14}/></button></div>}
          <div className="composer-area"><input type="file" ref={fileInput} multiple hidden accept=".txt,.md,.csv,.json,.py,.js,.jsx,.ts,.tsx,.html,.css,.xml,.yaml,.yml,.log,.ini,.toml,.sql,.c,.cpp,.h,.java,.rs,.go,.sh,.bat,.ps1,.pdf,.docx,.png,.jpg,.jpeg,.webp" onChange={uploadFiles}/>{attachments.length>0&&<div className="attachment-list">{attachments.map(a=><span className="attachment-chip" key={a.id}>{a.kind==='image'?<ImageIcon size={14}/>:<FileText size={14}/>}<span>{a.name}{a.truncated?'（已截取）':''}</span><button className="icon-button" aria-label={'移除附件 '+a.name} onClick={()=>setAttachments(old=>old.filter(x=>x.id!==a.id))}><X size={12}/></button></span>)}</div>}{recording&&<div className="dictation-status"><span className="record-dot"/>正在听写 · {dictation||'请对着麦克风说话，点击麦克风结束'}<small>Windows 本地中文识别</small></div>}<div className={'composer '+(busy?'is-busy':'')}><textarea ref={textarea} value={input} disabled={busy||recording||!ready} placeholder={connected?'输入消息，和玄武聊聊…':'启动 Ollama 后，点击右上角重新连接…'} aria-label="消息输入框" rows={2} onChange={e=>{setInput(e.target.value);manualVoice.pulse(.25);}} onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();send();}}}/><div className="composer-tools"><div className="input-actions"><button className="icon-button" type="button" title="上传附件（每个10MB，最多8个）" aria-label="上传附件" disabled={busy||uploading||recording||!ready} onClick={()=>fileInput.current?.click()}><Paperclip size={18}/></button><button className={'icon-button '+(recording?'recording':'')} type="button" title={recording?'结束语音输入':'语音输入'} aria-label={recording?'结束语音输入':'语音输入'} disabled={busy||uploading||speechPending||!ready} onClick={toggleDictation}>{recording?<MicOff size={18}/>:<Mic size={18}/>}</button><span className="composer-status">{uploading?'正在解析附件…':recording?'正在听写':'本地推理'}</span></div><span className="generation-status">{busy?<><span className="status-dot online"/>本地模型正在生成</>:<><Shield size={13}/> Ollama · 本地推理</>}</span>{busy?<button className="send-button stop" onClick={stop} title="停止生成" aria-label="停止生成"><Square size={17}/></button>:<button className="send-button" onClick={()=>send()} disabled={(!input.trim()&&!attachments.length)||!connected||!settings.model||!ready||uploading||recording||speechPending} title="发送消息" aria-label="发送消息"><ArrowUp size={21}/></button>}</div></div><div className="composer-footer"><span>Enter 发送 · Shift + Enter 换行</span><div>{speaking&&<button onClick={silence}>停止朗读</button>}{lastMessage?.error&&!busy&&<button onClick={()=>{const prior=current.messages.slice(0,-2);const question=current.messages.at(-2)?.content;setCurrent({...current,messages:prior});setInput(question||'');}}>重新编辑</button>}{current.messages.length>0&&<button title="导出 Markdown" onClick={exportChat}><Download size={12}/>导出</button>}</div></div></div>
        </section>
      </div>
    </main>
    {modal&&draft&&<div className="modal-backdrop" onClick={e=>{if(e.target===e.currentTarget&&!saving)setModal(false);}}><form className="settings-modal" onSubmit={saveSettings}>
      <div className="modal-heading"><div><span className="eyebrow">PREFERENCES</span><h2>设置与连接</h2></div><button type="button" className="icon-button" onClick={()=>setModal(false)} aria-label="关闭设置"><X size={20}/></button></div>
      <details className="settings-section" open><summary><Cpu size={16}/> Ollama 与外观</summary><label>Ollama 地址<input value={draft.endpoint} onChange={e=>setDraft({...draft,endpoint:e.target.value})} required/></label><p className="field-help">连接本机服务，默认端口 11434。</p><div className="settings-grid"><label>粒子质量<select value={draft.quality} onChange={e=>setDraft({...draft,quality:e.target.value})}><option value="low">节能 · 6,000 粒子</option><option value="medium">均衡 · 16,000 粒子</option><option value="high">精细 · 38,000 粒子</option></select></label><label>默认粒子角色<select value={draft.character} onChange={e=>setDraft({...draft,character:e.target.value})}>{Object.entries(characterNames).map(([key,name])=><option key={key} value={key}>{name}</option>)}</select></label></div><label className="check-label"><input type="checkbox" checked={draft.voice} onChange={e=>setDraft({...draft,voice:e.target.checked})}/><span>回复完成后自动朗读<small>使用系统语音；口型由 Qivi 的文本节奏驱动。</small></span></label></details>
      <details className="settings-section" open><summary><Shield size={16}/> 本机操作权限</summary><div className="permission-cards">{permissionModes.map(mode=><label key={mode.id} className={'permission-card '+(draft.permission_mode===mode.id?'chosen':'')}><input type="radio" name="permission-mode" value={mode.id} checked={draft.permission_mode===mode.id} onChange={e=>{if(mode.id==='full'&&!window.confirm('开启完全访问权限后，Agent 可直接操作电脑文件、命令行和已启用 MCP 工具。要继续吗？'))return;setDraft({...draft,permission_mode:e.target.value});}}/><span><strong>{mode.name}</strong><small>{mode.description}</small></span>{draft.permission_mode===mode.id&&<Check size={15}/>}</label>)}</div><label>文件工具工作目录<div className="folder-field"><input value={draft.workspace} onChange={e=>setDraft({...draft,workspace:e.target.value})} required/><button type="button" className="icon-button" aria-label="浏览工作目录" onClick={async()=>{try{const result=await api('pick-folder',{});if(result.path)setDraft({...draft,workspace:result.path});}catch(e){setError(e.message);}}}><FolderOpen size={18}/></button></div></label><p className="field-help">请求批准 / 帮我批准仅在此目录内操作；完全访问权限可访问电脑上的其他路径。覆盖文件会自动备份。</p><label className="check-label"><input type="checkbox" checked={draft.tools_enabled} onChange={e=>setDraft({...draft,tools_enabled:e.target.checked})}/><span>启用本地文件工具<small>查看、搜索、创建、修改、移动文件和文件夹。</small></span></label><label className="check-label"><input type="checkbox" checked={draft.shell_enabled} onChange={e=>setDraft({...draft,shell_enabled:e.target.checked})}/><span><Terminal size={14}/> 启用命令行<small>Agent 可按当前权限档位运行本机命令并读取执行结果。</small></span></label></details>
      <details className="settings-section" open><summary><Users size={16}/> 多 Agent 与 Qivi 角色</summary><div className="agent-editor"><div className="agent-editor-select"><label>正在编辑<select value={editingAgent.id} onChange={e=>setAgentEditorId(e.target.value)}>{draftAgents.map(agent=><option key={agent.id} value={agent.id}>{agent.name} · {characterNames[agent.character]}</option>)}</select></label><span className="agent-editor-avatar"><QiviAvatar size="88px" character={CHARACTERS[editingAgent.character]} personality="guardian" appearance={draft.theme==='light'?'light':'dark'} theme="arctic" quality="low" state="idle" interactive={false} lighting={0.9} intensity={0.7} autoSleep={false} label={`${editingAgent.name} Qivi 角色预览`}/></span></div><label>Agent 名称<input value={editingAgent.name} onChange={e=>updateDraftAgent('name',e.target.value)} maxLength={40}/></label><label>独立粒子角色<select value={editingAgent.character} onChange={e=>updateDraftAgent('character',e.target.value)}>{Object.entries(characterNames).filter(([key])=>!draftAgents.some(agent=>agent.character===key&&agent.id!==editingAgent.id)).map(([key,name])=><option key={key} value={key}>{name}</option>)}</select></label><label>Agent 专属指令<textarea rows={3} value={editingAgent.system} onChange={e=>updateDraftAgent('system',e.target.value)}/></label><div className="agent-editor-actions"><button type="button" onClick={addAgent} disabled={draftAgents.length>=6}><Plus size={14}/>新增 Agent</button><button type="button" onClick={removeAgent} disabled={draftAgents.length<=1}><Trash2 size={14}/>删除当前 Agent</button><span>不同 Agent 使用独立提示词与 Qivi 粒子角色；主 Agent 可调用其他 Agent 协作。</span></div></div><label>通用系统指令<textarea rows={3} value={draft.system} onChange={e=>setDraft({...draft,system:e.target.value})}/></label></details>
      <details className="settings-section" open><summary><Sparkles size={16}/> Skills <button type="button" onClick={e=>{e.preventDefault();refreshIntegrations();}}><RotateCw size={12}/>刷新</button></summary><p className="field-help">发现工作目录和用户目录中的 .agents/skills、.claude/skills、.minimax/skills。</p><div className="integration-list">{availableSkills.length?availableSkills.map(skill=><label className="integration-row" key={skill.id}><input type="checkbox" checked={(draft.skills_enabled||[]).includes(skill.id)} onChange={e=>setDraft({...draft,skills_enabled:e.target.checked?[...(draft.skills_enabled||[]),skill.id]:(draft.skills_enabled||[]).filter(id=>id!==skill.id)})}/><span><strong>{skill.name}</strong><small>{skill.description||skill.path}</small></span></label>):<div className="integration-empty">未发现 Skill。把包含 SKILL.md 的文件夹放入工作目录 .agents/skills 后刷新。</div>}</div></details>
      <details className="settings-section" open><summary><Plug size={16}/> MCP 服务</summary><p className="field-help">支持 stdio 和 Streamable HTTP。已启用服务的工具会加入 Agent 工具列表；没有只读标注的操作会按权限档位询问。</p><div className="mcp-list">{(draft.mcp_servers||[]).map(server=><div className="mcp-row" key={server.name}><label className="check-label"><input type="checkbox" checked={server.enabled!==false} onChange={e=>setDraft({...draft,mcp_servers:draft.mcp_servers.map(item=>item.name===server.name?{...item,enabled:e.target.checked}:item)})}/><span><strong>{server.name}</strong><small>{server.transport==='stdio'?`${server.command} ${(server.args||[]).join(' ')}`:server.url}</small></span></label><button type="button" className="icon-button" aria-label={`移除 MCP ${server.name}`} onClick={()=>setDraft({...draft,mcp_servers:draft.mcp_servers.filter(item=>item.name!==server.name)})}><Trash2 size={14}/></button></div>)}</div><div className="mcp-status-list">{mcpStatus.map(server=><div className={'mcp-status '+(server.error?'error':'')} key={server.name}><span>{server.error?<CircleX size={13}/>:<CircleCheck size={13}/>}</span><strong>{server.name}</strong>{server.error?<small>{server.error}</small>:<small>{server.tools?.length||0} 个工具</small>}</div>)}</div><div className="mcp-add-form"><label>服务名称<input value={mcpDraft.name} onChange={e=>setMcpDraft({...mcpDraft,name:e.target.value})} placeholder="例如 filesystem"/></label><label>连接方式<select value={mcpDraft.transport} onChange={e=>setMcpDraft({...mcpDraft,transport:e.target.value})}><option value="stdio">本地进程 · stdio</option><option value="streamable_http">网络服务 · Streamable HTTP</option></select></label>{mcpDraft.transport==='stdio'?<><label>启动命令<input value={mcpDraft.command} onChange={e=>setMcpDraft({...mcpDraft,command:e.target.value})} placeholder="npx"/></label><label>命令参数（JSON 数组）<textarea rows={2} value={mcpDraft.args} onChange={e=>setMcpDraft({...mcpDraft,args:e.target.value})} placeholder='["-y", "@modelcontextprotocol/server-filesystem", "C:\\Users"]'/></label><label>进程工作目录<input value={mcpDraft.cwd} onChange={e=>setMcpDraft({...mcpDraft,cwd:e.target.value})}/></label><label>环境变量（JSON 对象）<textarea rows={2} value={mcpDraft.env} onChange={e=>setMcpDraft({...mcpDraft,env:e.target.value})} placeholder='{"API_KEY":"..."}'/></label></>:<><label>服务器 URL<input value={mcpDraft.url} onChange={e=>setMcpDraft({...mcpDraft,url:e.target.value})} placeholder="http://127.0.0.1:8000/mcp"/></label><label>HTTP 请求头（JSON 对象）<textarea rows={2} value={mcpDraft.headers} onChange={e=>setMcpDraft({...mcpDraft,headers:e.target.value})} placeholder='{"Authorization":"Bearer ..."}'/></label></>}<button type="button" className="secondary-button" onClick={addMcpServer}><Plus size={14}/>添加 MCP 服务</button></div></details>
      {error&&<p className="message-error">{error}</p>}<div className="modal-footer"><span>Qivi 0.1.2 · 本地运行</span><button type="submit" className="primary-button" disabled={saving}>{saving?'正在保存…':'保存设置'}</button></div>
    </form></div>}
  </div>;
}
createRoot(document.getElementById('root')).render(<App/>);
