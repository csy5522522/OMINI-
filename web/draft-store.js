'use strict';
// OAuth drafts are encrypted in IndexedDB. CryptoKeys remain non-extractable;
// neither credentials nor conflict snapshots are written to localStorage.
const cloudDraftStore = {
  async database() {
    if (!globalThis.indexedDB || !globalThis.crypto?.subtle) throw new Error('此浏览器未启用加密草稿存储。');
    return new Promise((resolve,reject)=>{
      const request=indexedDB.open('omini-account-drafts',1);
      request.onupgradeneeded=()=>request.result.createObjectStore('workspaces');
      request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);
    });
  },
  async entry(owner) {
    const db=await this.database();
    try{return await new Promise((resolve,reject)=>{const r=db.transaction('workspaces').objectStore('workspaces').get(owner);r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);});}
    finally{db.close();}
  },
  async load(owner) {
    const record=await this.entry(owner);if(!record)return {};
    const bytes=await crypto.subtle.decrypt({name:'AES-GCM',iv:record.iv,additionalData:new TextEncoder().encode(owner)},record.key,record.body);
    return JSON.parse(new TextDecoder().decode(bytes));
  },
  async save(owner,value) {
    const old=await this.entry(owner),key=old?.key||await crypto.subtle.generateKey({name:'AES-GCM',length:256},false,['encrypt','decrypt']);
    const iv=crypto.getRandomValues(new Uint8Array(12));
    const body=await crypto.subtle.encrypt({name:'AES-GCM',iv,additionalData:new TextEncoder().encode(owner)},key,new TextEncoder().encode(JSON.stringify(value)));
    const db=await this.database();
    try{await new Promise((resolve,reject)=>{const tx=db.transaction('workspaces','readwrite');tx.objectStore('workspaces').put({key,iv,body},owner);tx.oncomplete=resolve;tx.onerror=()=>reject(tx.error);});}
    finally{db.close();}
  }
};
