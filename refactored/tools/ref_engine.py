"""Executable Python reference for the documented conservative OHLC execution model.
This is NOT a Pine interpreter or proof of Pine runtime parity. No signal optimization.
"""
from __future__ import annotations
from dataclasses import dataclass, field, replace, asdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from math import isfinite, floor, ceil
from typing import Optional, Iterable

EPS = 1e-10

@dataclass(frozen=True)
class Bar:
    index: int
    time: int
    time_close: int
    open: float
    high: float
    low: float
    close: float
    def __post_init__(self):
        if not all(isfinite(x) for x in (self.open,self.high,self.low,self.close)):
            raise ValueError('Non-finite OHLC')
        if self.index < 0 or self.time_close <= self.time or self.low > min(self.open,self.close) or self.high < max(self.open,self.close) or self.low > self.high:
            raise ValueError('Invalid OHLC/time ordering')

@dataclass(frozen=True)
class Plan:
    id: str
    family: int
    side: int
    signal_bar: int
    signal_time: int
    reference: float
    stop: float
    targets: tuple[float,float,float]
    atr: float
    cost_price: float
    max_risk_atr: float = 3.5
    obstacle: Optional[float] = None
    required_room: float = 0.0
    countertrend: bool = False
    eligible: bool = False
    evidence: str = 'UNVALIDATED'
    def __post_init__(self):
        values = (self.reference,self.stop,*self.targets,self.atr,self.cost_price,self.max_risk_atr)
        if not self.id or self.family not in (1,2,3) or self.side not in (-1,1) or not all(isfinite(x) for x in values):
            raise ValueError('Invalid plan')
        if self.atr <= 0 or self.cost_price < 0 or self.max_risk_atr <= 0:
            raise ValueError('Invalid risk/cost')
        prices = (self.stop,self.reference,*self.targets)
        if any((b-a)*self.side <= 0 for a,b in zip(prices,prices[1:])):
            raise ValueError('SL/reference/targets must be strictly ordered')
        if self.required_room < 0 or (self.obstacle is not None and not isfinite(self.obstacle)):
            raise ValueError('Invalid obstacle')

@dataclass(frozen=True)
class Config:
    tick: float = .001
    min_stop_atr: float = .8
    max_open_deviation_atr: float = .20
    max_cost_risk: float = .18
    gap_filter: bool = True
    profit_lock_r: float = .05
    max_hold_bars: int = 48
    timezone: str = 'Europe/Sofia'
    funding_long: float = 0.0
    funding_short: float = 0.0
    manual_news_pause: bool = False
    news_windows: tuple[tuple[int,int], ...] = ()
    shield_enabled: bool = True
    global_min_closed: int = 30
    global_stop_net_r: float = -5.0
    model_min_closed: int = 20
    model_stop_average_r: float = -.15
    recovery_trades: int = 5
    countertrend_observe_only: bool = True
    daily_loss_r: float = 3.0
    daily_losing_streak: int = 3
    def __post_init__(self):
        ZoneInfo(self.timezone)
        positive = (self.tick,self.min_stop_atr,self.max_open_deviation_atr,self.max_cost_risk,self.daily_loss_r)
        if not all(isfinite(x) and x>0 for x in positive) or not all(isfinite(x) and x>=0 for x in (self.profit_lock_r,self.funding_long,self.funding_short)) or self.max_hold_bars<1:
            raise ValueError('Invalid configuration')
        if self.global_min_closed<1 or self.model_min_closed<1 or self.recovery_trades<1 or self.daily_losing_streak<1:
            raise ValueError('Invalid count limits')
        if self.global_stop_net_r>=0 or self.model_stop_average_r>=0:
            raise ValueError('Shield thresholds must be negative')
        if any(b<a for a,b in self.news_windows):
            raise ValueError('Invalid news window')

def day_key(ms: int, tz: str) -> str:
    return datetime.fromtimestamp(ms/1000,timezone.utc).astimezone(ZoneInfo(tz)).date().isoformat()

def stop_round(x: float, side: int, tick: float) -> float:
    if tick<=0 or side not in (-1,1) or not isfinite(x): raise ValueError('Bad tick/side/price')
    return (floor(x/tick+1e-8) if side==1 else ceil(x/tick-1e-8))*tick

def target_round(x: float, side: int, tick: float) -> float:
    return -stop_round(-x,side,tick)

def nearest_obstacle(values: Iterable[Optional[float]], side: int, price: float) -> Optional[float]:
    legal=[v for v in values if v is not None and isfinite(v) and (v-price)*side>0]
    return min(legal,key=lambda v:(v-price)*side) if legal else None

def valid_plan(p: Plan, c: Config) -> bool:
    risk=(p.reference-p.stop)*p.side
    return risk>=c.tick and risk+c.tick*1e-6>=c.min_stop_atr*p.atr and risk<=p.max_risk_atr*p.atr and p.cost_price/risk<=c.max_cost_risk

@dataclass
class Ledger:
    entered: int = 0
    closed: int = 0
    positive: int = 0
    negative: int = 0
    zero: int = 0
    cancelled: int = 0
    capacity_skipped: int = 0
    ambiguous: int = 0
    gross: float = 0
    costs: float = 0
    net: float = 0
    gains: float = 0
    losses: float = 0
    peak: float = 0
    drawdown: float = 0
    hold_bars: int = 0
    def record(self,gross:float,costs:float,hold:int):
        net=gross-costs
        self.closed+=1;self.gross+=gross;self.costs+=costs;self.net+=net;self.hold_bars+=hold
        if net>EPS:self.positive+=1;self.gains+=net
        elif net<-EPS:self.negative+=1;self.losses-=net
        else:self.zero+=1
        self.peak=max(self.peak,self.net);self.drawdown=max(self.drawdown,self.peak-self.net)

@dataclass
class Gate:
    paused: bool = False
    paused_at: int = 0
    def update(self,l:Ledger,c:Config,global_gate:bool)->bool:
        previous=self.paused
        minimum=c.global_min_closed if global_gate else c.model_min_closed
        value=l.net if global_gate else l.net/l.closed if l.closed else 0
        threshold=c.global_stop_net_r if global_gate else c.model_stop_average_r
        recovery=threshold*.5 if global_gate else 0
        if not c.shield_enabled:self.paused=False
        elif l.closed>=minimum:
            if not self.paused and value<=threshold:self.paused=True;self.paused_at=l.closed
            elif self.paused and l.closed>=self.paused_at+c.recovery_trades and value>=recovery:self.paused=False
        return previous != self.paused

@dataclass
class Paper:
    name: str
    config: Config
    ledger: Ledger = field(default_factory=Ledger)
    phase: int = 0
    plan: Optional[Plan] = None
    entry: float = 0
    risk: float = 0
    stop: float = 0
    remaining: float = 1
    hits: list[bool] = field(default_factory=lambda:[False]*3)
    fill_bar: int = -1
    fill_time: int = 0
    lock_bar: Optional[int] = None
    exit_next_open: bool = False
    gross: float = 0
    costs: float = 0
    day: str = ''
    day_net: float = 0
    day_streak: int = 0
    last_closed: Optional[dict] = None
    last_cancelled: Optional[dict] = None
    events: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    partials: list[dict] = field(default_factory=list)
    _ids: set[str] = field(default_factory=set)
    def emit(self,bar:Bar,kind:str,price:Optional[float]=None,effective:Optional[int]=None,**extra):
        assert self.plan is not None
        eid=f'{self.plan.id}|{self.name}|{kind}|{bar.time_close}'
        if eid in self._ids:return
        self._ids.add(eid)
        event={'event_id':eid,'event':kind,'book':self.name,'plan_id':self.plan.id,'family':self.plan.family,'side':self.plan.side,'effective_time':effective,'known_time':bar.time_close,'price':price,'evidence':self.plan.evidence,**extra}
        self.events.append(event)
    def queue(self,p:Plan,bar:Bar)->bool:
        if p.signal_bar!=bar.index or p.signal_time!=bar.time_close:raise ValueError('Cannot backdate a plan')
        if self.phase:
            self.ledger.capacity_skipped+=1
            self.events.append({'event':'CAPACITY','book':self.name,'plan_id':p.id,'known_time':bar.time_close})
            return False
        self.plan=p;self.phase=1;self.emit(bar,'QUEUED',p.reference,bar.time_close);return True
    def realize(self,bar:Bar,fraction:float,price:float,kind:str,effective:Optional[int]=None):
        assert self.plan is not None and self.phase==2 and self.risk>0
        part=min(fraction,self.remaining)
        if part<=EPS:raise ValueError('Exit without remaining quantity')
        gross=part*(price-self.entry)*self.plan.side/self.risk
        rate=self.config.funding_long if self.plan.side==1 else self.config.funding_short
        end=bar.time_close if effective is None else effective
        costs=part*(self.plan.cost_price+rate*max(0,(end-self.fill_time)/86400000))/self.risk
        self.remaining=max(0,self.remaining-part);self.gross+=gross;self.costs+=costs;self.day_net+=gross-costs
        item={'quantity_fraction':part,'gross_r':gross,'cost_r':costs,'net_r':gross-costs,'price':price,'effective_time':effective,'known_time':bar.time_close,'reason':kind}
        self.partials.append(item)
        self.emit(bar,kind,price,effective,quantity_fraction=part,gross_r=gross,cost_r=costs,net_r=gross-costs)
    def finish(self,bar:Bar,reason:str):
        assert self.plan is not None and self.remaining<EPS
        net=self.gross-self.costs;self.ledger.record(self.gross,self.costs,bar.index-self.fill_bar)
        if net<-EPS:self.day_streak+=1
        elif net>EPS:self.day_streak=0
        trade={'plan_id':self.plan.id,'book':self.name,'family':self.plan.family,'side':self.plan.side,'signal_time':self.plan.signal_time,'modeled_fill_time':self.fill_time,'modeled_fill_price':self.entry,'initial_sl':self.plan.stop,'targets':list(self.plan.targets),'exit_known_time':bar.time_close,'exit_effective_time':self.partials[-1]['effective_time'],'exit_price':self.partials[-1]['price'],'exit_reason':reason,'initial_risk_price':self.risk,'gross_r':self.gross,'cost_r':self.costs,'net_r':net,'hold_bars':bar.index-self.fill_bar,'evidence':self.plan.evidence,'partials':list(self.partials)}
        self.trades.append(trade);self.last_closed=trade
        self.emit(bar,'CLOSED',trade['exit_price'],trade['exit_effective_time'],gross_r=self.gross,cost_r=self.costs,net_r=net,reason=reason)
        self.phase=0;self.exit_next_open=False;self.lock_bar=None
    def step(self,b:Bar):
        today=day_key(b.time,self.config.timezone)
        if self.day!=today:self.day=today;self.day_net=0;self.day_streak=0
        c=self.config;p=self.plan
        if p is None or self.phase==0:return
        if self.phase==1 and b.index>p.signal_bar:
            r=(b.open-p.stop)*p.side
            wrong=r<c.tick or (p.targets[0]-b.open)*p.side<=0
            far=abs(b.open-p.reference)>p.atr*c.max_open_deviation_atr
            risk_ok=r>=c.min_stop_atr*p.atr and r<=p.max_risk_atr*p.atr and r>0 and p.cost_price/r<=c.max_cost_risk
            room=p.required_room<=0 or p.obstacle is not None and (p.obstacle-b.open)*p.side>=r*p.required_room
            news=c.manual_news_pause or any(a<=b.time<=z for a,z in c.news_windows)
            if wrong or news or c.gap_filter and (far or not risk_ok or not room):
                why='WRONG_SIDE' if wrong else 'NEWS' if news else 'GAP' if far else 'RISK_COST' if not risk_ok else 'ROOM'
                self.last_cancelled={'plan_id':p.id,'reason':why,'time':b.time};self.ledger.cancelled+=1
                self.emit(b,'CANCELLED',b.open,b.time,reason=why);self.phase=0;return
            self.phase=2;self.entry=b.open;self.risk=r;self.stop=p.stop;self.remaining=1;self.hits=[False]*3
            self.fill_bar=b.index;self.fill_time=b.time;self.lock_bar=None;self.exit_next_open=False;self.gross=0;self.costs=0;self.partials=[]
            self.ledger.entered+=1;self.emit(b,'SIM_ENTRY',b.open,b.time,quantity_fraction=1)
        if self.phase!=2:return
        if self.lock_bar is not None and b.index>=self.lock_bar:
            lock=stop_round(self.entry+p.side*self.risk*c.profit_lock_r,p.side,c.tick)
            new=max(self.stop,lock) if p.side==1 else min(self.stop,lock)
            if new!=self.stop:self.stop=new;self.emit(b,'STOP_UPDATE',new,b.time)
            self.lock_bar=None
        if self.exit_next_open:
            gap=(b.open-self.stop)*p.side<=0
            self.realize(b,self.remaining,b.open,'SIM_SL' if gap else 'SIM_TIME',b.time)
            self.finish(b,'STOP_GAP' if gap else 'TIME_NEXT_OPEN');return
        stop_touched=b.low<=self.stop if p.side==1 else b.high>=self.stop
        touched=[b.high>=t if p.side==1 else b.low<=t for t in p.targets]
        if stop_touched:
            if any(t and not h for t,h in zip(touched,self.hits)):
                self.ledger.ambiguous+=1;self.emit(b,'AMBIGUOUS',self.stop)
            price=min(b.open,self.stop) if p.side==1 else max(b.open,self.stop)
            effective=b.time if (b.open-self.stop)*p.side<=0 else None
            self.realize(b,self.remaining,price,'SIM_SL',effective);self.finish(b,'STOP_FIRST');return
        for i,touch in enumerate(touched):
            if touch and not self.hits[i]:
                self.realize(b,self.remaining if i==2 else 1/3,p.targets[i],f'SIM_TP{i+1}')
                self.hits[i]=True
                if i==0:self.lock_bar=b.index+1
                if i==2:self.finish(b,'TP3');return
        if b.index-self.fill_bar>=c.max_hold_bars:
            self.exit_next_open=True;self.emit(b,'TIME_PENDING',b.close,b.time_close)

def pick(plans:list[Plan],family:int=0,eligible_only:bool=False)->Optional[Plan]:
    choices=[p for p in plans if (not family or p.family==family) and (not eligible_only or p.eligible)]
    if len({p.side for p in choices})!=1:return None
    return min(choices,key=lambda p:p.family)

class Engine:
    """Five independent execution books fed by already-confirmed, timestamped candidates."""
    def __init__(self,c:Config):
        self.c=c;self.books={name:Paper(name,c) for name in ('M1','M2','M3','RAW','FILTERED')}
        self.gates={name:Gate() for name in ('RAW','M1','M2','M3')};self.previous_time=None;self.last_index=-1
    def process(self,b:Bar,plans:list[Plan]):
        if b.index<=self.last_index or self.previous_time is not None and b.time<self.previous_time:
            raise ValueError('Bars must be sequential without overlap')
        if len({p.id for p in plans})!=len(plans):raise ValueError('Duplicate candidate id')
        self.last_index=b.index;self.previous_time=b.time_close
        for book in self.books.values():book.step(b)
        for name,gate in self.gates.items():gate.update(self.books[name].ledger,self.c,name=='RAW')
        filtered=self.books['FILTERED'];daily=filtered.day_net>-self.c.daily_loss_r and filtered.day_streak<self.c.daily_losing_streak
        out=[]
        for p in plans:
            if not valid_plan(p,self.c):raise ValueError('Invalid candidate risk/targets')
            paused=self.c.shield_enabled and (self.gates['RAW'].paused or self.gates[f'M{p.family}'].paused)
            contra=self.c.countertrend_observe_only and p.countertrend
            status='PAUSED_EVIDENCE' if paused else 'RESEARCH_COUNTERTREND' if contra else 'PAUSED_DAILY' if not daily else 'UNVALIDATED_SMALL_SAMPLE' if self.books[f'M{p.family}'].ledger.closed<self.c.model_min_closed else 'UNVALIDATED_NO_OOS'
            out.append(replace(p,eligible=not paused and not contra and daily,evidence=status))
        for i in range(1,4):
            chosen=pick(out,i)
            if chosen:self.books[f'M{i}'].queue(chosen,b)
        for name,filtered_only in [('RAW',False),('FILTERED',True)]:
            chosen=pick(out,eligible_only=filtered_only)
            if chosen:self.books[name].queue(chosen,b)
        return out

@dataclass
class SetupLifecycle:
    """Reference lifecycle used for synthetic tests; not a port of the indicator feature model."""
    family:int
    side:int
    phase:int=0
    born:int=-1
    mss:int=-1
    id:str=''
    last_invalid:int=-1
    def cancel(self,bar:int):self.phase=0;self.last_invalid=bar
    def start(self,bar:int,created_time:int)->bool:
        if self.phase or self.last_invalid==bar:return False
        self.phase=1;self.born=bar;self.id=f'{self.family}|{self.side}|{created_time}';return True
    def shift(self,bar:int)->bool:
        if self.phase!=1 or bar<=self.born:return False
        self.phase=2;self.mss=bar;return True
    def can_confirm(self,bar:int,max_age:int)->bool:
        return self.phase==2 and self.mss<bar<=self.mss+max_age

def allowed_event(kind:str,book:str,master:bool=True,watch:bool=True,plan:bool=True,risk:bool=True,paper:bool=True,selected:str='RAW')->bool:
    if not master:return False
    paper_book=book=='NATIVE' or book==selected or selected=='BOTH' and book in ('RAW','FILTERED')
    risk_kinds={'CANCELLED','EXPIRED','REJECTED','EVIDENCE','CONFLICT','AMBIGUOUS','NATIVE_DIVERGENCE','NATIVE_NOT_FILLED'}
    native_execution=kind in {'NATIVE_SUBMITTED','NATIVE_ENTRY','NATIVE_CLOSED'} or kind.startswith('NATIVE_EXIT_')
    return (book=='CORE' and kind=='WATCH' and watch or book=='CORE' and kind=='CONFIRMED' and plan or (book=='CORE' or paper_book) and kind in risk_kinds and risk or paper_book and (kind.startswith('SIM_') or native_execution or kind in {'STOP_UPDATE','TIME_PENDING','CLOSED'}) and paper)
