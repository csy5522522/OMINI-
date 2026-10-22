import {Schema, Slice, Fragment} from 'prosemirror-model';
import {EditorState, TextSelection} from 'prosemirror-state';
import {EditorView} from 'prosemirror-view';
import {history, undo, redo, closeHistory} from 'prosemirror-history';
import {keymap} from 'prosemirror-keymap';
import {baseKeymap} from 'prosemirror-commands';

const assetPattern=/!\[([^\]\n]*)\]\(omini-asset:\/\/([a-f0-9]{64})\)/g;
const schema=new Schema({nodes:{
  doc:{content:'paragraph'},
  paragraph:{content:'inline*',toDOM:()=>['p',0],parseDOM:[{tag:'p'}]},
  text:{group:'inline'},
  hard_break:{inline:true,group:'inline',selectable:false,toDOM:()=>['br'],parseDOM:[{tag:'br'}]},
  image:{inline:true,group:'inline',atom:true,attrs:{id:{},alt:{},token:{}},
    toDOM:node=>['span',{'data-memo-image':node.attrs.id},node.attrs.alt]}
}});
function parseText(text){
  const nodes=[];
  function plain(value){value.split('\n').forEach((line,i)=>{if(i)nodes.push(schema.nodes.hard_break.create());if(line)nodes.push(schema.text(line));});}
  let start=0;
  for(const match of text.matchAll(assetPattern)){
    plain(text.slice(start,match.index));
    nodes.push(schema.nodes.image.create({id:match[2],alt:match[1],token:match[0]}));
    start=match.index+match[0].length;
  }
  plain(text.slice(start));return nodes;
}
function serialize(fragment){
  let result='';fragment.forEach(node=>{if(node.isText)result+=node.text;else if(node.type.name==='image')result+=node.attrs.token;else if(node.type.name==='hard_break')result+='\n';else result+=serialize(node.content);});return result;
}
export function create(root,options={}){
  let readOnly=true,revision=0,disposed=false;
  const bookmarks=new Set();
  const makeState=text=>EditorState.create({schema,doc:schema.node('doc',null,[schema.node('paragraph',null,parseText(text))]),plugins:[
    history(),keymap({'Mod-z':undo,'Shift-Mod-z':redo,'Mod-y':redo,
      'Mod-End':(state,dispatch)=>{if(dispatch)dispatch(state.tr.setSelection(TextSelection.atEnd(state.doc)).scrollIntoView());return true;},
      'Mod-Home':(state,dispatch)=>{if(dispatch)dispatch(state.tr.setSelection(TextSelection.atStart(state.doc)).scrollIntoView());return true;},
      Enter:(state,dispatch)=>{if(dispatch)dispatch(state.tr.replaceSelectionWith(schema.nodes.hard_break.create()).scrollIntoView());return true;},
      'Shift-Enter':(state,dispatch)=>{if(dispatch)dispatch(state.tr.replaceSelectionWith(schema.nodes.hard_break.create()).scrollIntoView());return true;}}),keymap(baseKeymap)
  ]});
  let view=new EditorView(root,{
    state:makeState(''),editable:()=>!readOnly,
    attributes:{role:'textbox','aria-label':'备忘录正文','aria-multiline':'true','data-placeholder':'写下想记住的事…'},
    dispatchTransaction(tr){
      for(const point of bookmarks){point.from=tr.mapping.map(point.from,1);point.to=tr.mapping.map(point.to,-1);}
      view.updateState(view.state.apply(tr));
      if(tr.docChanged){root.classList.toggle('is-empty',!getContent());options.onChange?.();}
    },
    clipboardTextSerializer:slice=>serialize(slice.content),
    clipboardTextParser:text=>new Slice(Fragment.from(parseText(text)),0,0),
    handlePaste(editor,event){
      const file=[...(event.clipboardData?.files||[])].find(f=>f.type.startsWith('image/'));
      if(file){options.onImage?.(file);return true;}
      const text=event.clipboardData?.getData('text/plain');
      if(text!==undefined){editor.dispatch(editor.state.tr.replaceSelection(new Slice(Fragment.from(parseText(text)),0,0)).scrollIntoView());return true;}
      return false;
    },
    handleDrop(editor,event){
      const file=[...(event.dataTransfer?.files||[])].find(f=>f.type.startsWith('image/'));
      if(file){const pos=editor.posAtCoords({left:event.clientX,top:event.clientY});if(pos)editor.dispatch(editor.state.tr.setSelection(TextSelection.create(editor.state.doc,pos.pos)));options.onImage?.(file);return true;}
      return true;
    },
    handleDOMEvents:{compositionstart(){options.onComposition?.(true);},compositionend(){setTimeout(()=>options.onComposition?.(false),0);}},
    nodeViews:{image(node){
      let active=true;
      const dom=document.createElement('span');dom.className='memo-inline-image';dom.contentEditable='false';
      const img=document.createElement('img');img.alt=node.attrs.alt||'备忘录图片';img.draggable=false;
      const retry=document.createElement('button');retry.type='button';retry.className='memo-image-retry';retry.textContent='图片暂未加载 · 点击重试';retry.hidden=true;
      dom.append(img,retry);
      const load=async()=>{retry.hidden=true;dom.classList.add('loading');try{const src=await options.loadAsset(node.attrs.id);if(active){img.src=src;}}catch(_){if(active){retry.hidden=false;dom.classList.remove('loading');}}};
      img.addEventListener('load',()=>dom.classList.remove('loading'));
      img.addEventListener('error',()=>{retry.hidden=false;dom.classList.remove('loading');});
      retry.addEventListener('click',load);load();
      return {dom,stopEvent:event=>event.target===retry,ignoreMutation:()=>true,destroy(){active=false;}};
    }}
  });
  function getContent(){return serialize(view.state.doc.content);}
  return {
    getContent,
    loadContent(text){revision++;bookmarks.clear();view.updateState(makeState(text));root.classList.toggle('is-empty',!text);},
    setReadOnly(value){readOnly=value;view.setProps({editable:()=>!readOnly});view.dom.setAttribute('aria-readonly',String(value));},
    bookmark(){const point={from:view.state.selection.from,to:view.state.selection.to,revision};bookmarks.add(point);return point;},
    releaseBookmark(point){bookmarks.delete(point);},
    insertImage(asset,point){
      if(readOnly||disposed||point&&point.revision!==revision)return false;
      const from=point?.from??view.state.selection.from,to=point?.to??view.state.selection.to;
      const alt=String(asset.name||'图片').replace(/[\]\n]/g,'_'),token=`![${alt}](omini-asset://${asset.id})`;
      const tr=closeHistory(view.state.tr).replaceWith(from,Math.max(from,to),schema.nodes.image.create({id:asset.id,alt,token}));
      tr.setSelection(TextSelection.near(tr.doc.resolve(from+1)));
      view.dispatch(tr.scrollIntoView());view.focus();if(point)bookmarks.delete(point);return true;
    },
    focus(){view.focus();},
    restoreSelection(from,to){const max=view.state.doc.content.size-1;view.dispatch(view.state.tr.setSelection(TextSelection.create(view.state.doc,Math.min(from,max),Math.min(to,max))));},
    isComposing(){return view.composing;},
    destroy(){disposed=true;view.destroy();bookmarks.clear();},
    view
  };
}
