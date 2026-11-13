// "Get this data from EDI" (plan step U19): in place of a CSV of the values on screen, a Python or R
// script that downloads the source files from EDI, checks them against the EML and rebuilds the values
// (backend/edi_scripts.py). MATLAB users get EDI's own generated reader from the package page.
export function ediMenuHTML(id){
 return `<details class="edi-menu" id="${id}"><summary data-i18n="ui.edi_get">Get this data from EDI</summary>`+
  `<p data-i18n="ui.edi_explain">A script that downloads the source files from EDI, checks them against the EDI metadata, and rebuilds the values on this page as a CSV. The citation and the data rights are at the top.</p>`+
  `<ul><li><a id="${id}-python" download data-i18n="ui.edi_python">Python script</a></li>`+
  `<li><a id="${id}-r" download data-i18n="ui.edi_r">R script</a></li>`+
  `<li><a id="${id}-matlab" target="_blank" rel="noopener" data-i18n="ui.edi_matlab">MATLAB: the EDI package page, under Code Generation, reads the source files</a></li></ul></details>`;
}

export function ediScriptURL(view,lang,params={}){
 const q=new URLSearchParams({view,lang});
 for(const [k,v] of Object.entries(params))q.set(k,v);
 return '/api/edi-script?'+q;
}

// Point the menu at the current selection. landing is the EDI package page.
export function setEdiLinks(root,id,view,params,landing){
 const link=suffix=>root.querySelector(`#${id}-${suffix}`);
 for(const lang of ['python','r'])link(lang).href=ediScriptURL(view,lang,params);
 link('matlab').href=landing;
}
