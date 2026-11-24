import {ApiError,background,command,get,sessionChanged} from './api.js';
import {area,button,el,field,formatDate,formDialog,labelOf,plural,readFailed,select} from './dom.js';
import {peopleChoices,type Context} from './forms.js';
import type {LeaveAllowanceLine,LeaveBalance,LeaveBalanceSettings} from './generated.js';

const calendarDays=(days:number)=>(days<0?'-':'')+plural(Math.abs(days),'calendar day');

function leaveBalanceSection(balances:LeaveBalance[],projected=false){
  return el('section',{class:'panel request-section','data-private':''},el('h3',{},'Leave balance'),
    el('ul',{},...balances.map(balance=>el('li',{},
      el('strong',{},balance.leave_type_name),' ',calendarDays(balance.remaining_days),' remaining',
      projected&&balance.projected_days!=null?', '+calendarDays(balance.projected_days)+' after approval':'',
      ' in the leave year starting ',formatDate(balance.starts_on)))));
}

export function loadLeaveBalances(path:string,projected=false){
  const region=el('div',{hidden:true});
  const load=async()=>{
    try{
      const rows=await background<LeaveBalance[]>(path);
      if(region.isConnected){region.replaceChildren(...(rows.length?[leaveBalanceSection(rows,projected)]:[]));region.hidden=!rows.length;}
    }catch(error){
      if(region.isConnected&&!sessionChanged(error)){region.replaceChildren(readFailed('leave balances',error,load));region.hidden=false;}
    }
  };
  void load();
  return region;
}

export async function approveWithBalance<T>(requestId:string|undefined,send:(confirmations:Map<string,string>)=>Promise<T>,
    saved:(result:T)=>void|Promise<void>,confirmations=new Map<string,string>()):Promise<void>{
  let result:T;
  try{result=await send(confirmations);}
  catch(error){
    if(!(error instanceof ApiError)||error.code!=='LEAVE_BALANCE_CONFIRM')throw error;
    const details=error.details as {id?:string;balance_confirmation:string;balances:LeaveBalance[]};
    const id=details?.id||requestId;
    if(!id||!details.balance_confirmation||!details.balances?.length)throw error;
    formDialog('Confirm negative leave balance','',form=>{
      form.append(leaveBalanceSection(details.balances,true));
      return ()=>approveWithBalance(requestId,send,saved,new Map(confirmations).set(id,details.balance_confirmation));
    },'Approve below zero');
    return;
  }
  await saved(result);
}

export async function leaveAllowanceSettings(ctx:Context){
  const value=await get<LeaveBalanceSettings>('/api/leave-balance-settings'),configuration=value.configuration;
  const types=(ctx.catalogues.leave_types||[]).map(type=>({value:String(type.id),label:labelOf(type)}));
  const people=peopleChoices(ctx);
  formDialog('Leave allowances','',form=>{
    const month=field('Leave year start month','number',configuration.year_start_month??1,{required:true,min:1,max:12,step:1});
    const day=field('Leave year start day','number',configuration.year_start_day??1,{required:true,min:1,max:31,step:1});
    const carry=field('Maximum carry-over days','number',configuration.carry_over_days??0,{required:true,min:0,max:366,step:1});
    const rows=el('div',{class:'span-2'});
    const entries=new Map<HTMLElement,()=>LeaveAllowanceLine>();
    const add=(line?:LeaveAllowanceLine)=>{
      const type=select('Leave type',types,line?.leave_type_id||'',true);
      const person=select('Person',people,line?.person_id||'',false,'Everyone');
      const from=field('First leave year starting on or after','date',line?.starts_on||'');
      const days=field('Calendar days per year','number',line?.days??0,{required:true,min:0,max:366,step:1});
      const group=el('fieldset',{class:'form-grid'});
      const sync=()=>{from.wrap.hidden=!person.input.value;from.input.required=!!person.input.value;};
      person.input.addEventListener('change',sync);sync();
      group.append(el('legend',{},'Allowance'),type.wrap,days.wrap,person.wrap,from.wrap,
        button('Remove allowance',()=>{entries.delete(group);group.remove();},'ghost small'));
      rows.append(group);
      entries.set(group,()=>({leave_type_id:type.input.value,days:Number(days.input.value),
        person_id:person.input.value||null,starts_on:person.input.value?from.input.value:null}));
    };
    for(const line of configuration.allowances||[])add(line);
    const reason=area('Reason','');reason.input.required=true;reason.input.minLength=3;
    form.append(month.wrap,day.wrap,carry.wrap,rows,button('Add allowance',()=>add(),'secondary'),reason.wrap);
    return async()=>{
      await command('department.leave_balance_settings',{configuration:{
        year_start_month:Number(month.input.value),year_start_day:Number(day.input.value),
        carry_over_days:Number(carry.input.value),allowances:[...entries.values()].map(read=>read())},reason:reason.input.value},
        undefined,value.revision??undefined);
      await ctx.refresh();
    };
  },'Save allowances');
}
