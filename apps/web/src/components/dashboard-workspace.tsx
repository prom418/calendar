"use client";

import { AlertCircle, CalendarDays, ChevronRight, FileClock, RefreshCw, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { SourceHealthSummary } from "@/components/source-health";
import { type ApiChange, type ApiEvent, type AppSettings, fetchChanges, fetchEvent, fetchEvents } from "@/lib/api";
import { addDays, dateKeyInTimezone, formatDateRange, formatEventDateTime, formatEventTime, timezoneLabels, zonedDayRange } from "@/lib/date-time";
import { eventInstitution, eventOriginalTime, eventTitle } from "@/lib/event-title";
import { labels } from "@/lib/demo-events";
import { useLoadRetry } from "@/lib/use-load-retry";

import { usePreferences } from "./preferences-context";

export function DashboardWorkspace() {
  const { settings, readOnly, loading:preferencesLoading } = usePreferences();
  const [todayEvents, setTodayEvents] = useState<ApiEvent[]>([]);
  const [weekEvents, setWeekEvents] = useState<ApiEvent[]>([]);
  const [nextCritical, setNextCritical] = useState<ApiEvent|null>(null);
  const [changes, setChanges] = useState<Array<{ change:ApiChange; event:ApiEvent|null }>>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string|null>(null);
  const { attempt, retry } = useLoadRetry(Boolean(error), loading);

  useEffect(() => {
    // Wait until the access mode is known. `readOnly` starts as false, so
    // without this the first pass always called fetchChanges(), which the public
    // proxy rejects with 403 by design -- one guaranteed-failed request and a
    // console error on every dashboard load of the public site. Same guard as
    // live-changes-feed.tsx.
    if (preferencesLoading) return;
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError(null);
      const today = dateKeyInTimezone(new Date(), settings.timezone);
      const dayRange = zonedDayRange(today, settings.timezone);
      const weekEnd = zonedDayRange(addDays(today, 7), settings.timezone).start;
      const yearEnd = new Date(dayRange.start.getTime() + 370 * 86_400_000);
      try {
        const [day, week, critical, recentChanges] = await Promise.all([
          fetchEvents(new URLSearchParams({ from:dayRange.start.toISOString(), to:dayRange.end.toISOString(), from_date:today, to_date:addDays(today, 1), limit:"200" }).toString()),
          fetchEvents(new URLSearchParams({ from:dayRange.start.toISOString(), to:weekEnd.toISOString(), from_date:today, to_date:addDays(today, 7), limit:"200" }).toString()),
          fetchEvents(new URLSearchParams({ from:new Date().toISOString(), to:yearEnd.toISOString(), from_date:today, to_date:addDays(today, 370), importance:"critical", limit:"50" }).toString()),
          readOnly ? Promise.resolve([]) : fetchChanges(6),
        ]);
        const eventIds = [...new Set(recentChanges.map((change) => change.event_id))];
        const changeEvents = await Promise.all(eventIds.map(async (id) => {
          try { return await fetchEvent(id); } catch { return null; }
        }));
        if (cancelled) return;
        const eventById = new Map(eventIds.map((id, index) => [id, changeEvents[index]]));
        const watched = (event:ApiEvent) => event.market_tags.length === 0 || event.market_tags.some((market) => settings.markets.includes(market as AppSettings["markets"][number]));
        setTodayEvents(day.items.filter(watched));
        setWeekEvents(week.items.filter(watched));
        setNextCritical(critical.items.filter(watched).filter(isUpcoming).sort(compareEventTime)[0] ?? null);
        setChanges(recentChanges.map((change) => ({ change, event:eventById.get(change.event_id) ?? null })));
      } catch (reason) {
        if (!cancelled) {
          setTodayEvents([]);
          setWeekEvents([]);
          setNextCritical(null);
          setChanges([]);
          setError(reason instanceof Error ? reason.message : "总览数据加载失败");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();
    return () => { cancelled = true; };
  }, [readOnly, preferencesLoading, settings.markets, settings.timezone, attempt]);

  const upcomingToday = useMemo(() => todayEvents.filter(isUpcoming).length, [todayEvents]);
  const importantWeek = useMemo(() => weekEvents.filter((event) => event.importance === "critical" || event.importance === "high").slice(0, 3), [weekEvents]);
  return <>
    {error && <div className="data-error dashboard-error" role="alert"><AlertCircle size={16} /><span>{error}。连接恢复后将自动重试。</span><button onClick={retry} disabled={loading}>立即重试</button></div>}
    <section className="hero" aria-busy={loading}>
      <NextEventCard event={nextCritical} loading={loading} timezone={settings.timezone} />
      <div className="metrics">
        <article><span>今日真实事件 <CalendarDays size={18} /></span><strong>{loading || error ? "—" : todayEvents.length}</strong><p><b>{loading || error ? "—" : upcomingToday}</b> 个尚未发生</p></article>
        <article><span>最近变更 <FileClock size={18} /></span><strong>{loading || error ? "—" : changes.length}</strong><p>来自事件版本审计</p></article>
      </div>
    </section>

    <section className="content">
      <article className="panel timeline-panel">
        <div className="panel-head"><div><h2>今日时间线</h2><p>按当前时区排序</p></div><Link href="/today">查看今天 <ChevronRight size={15} /></Link></div>
        <div className="timeline">
          {!loading && !error && todayEvents.map((event) => <DashboardEvent key={event.id} event={event} timezone={settings.timezone} />)}
          {!loading && !error && todayEvents.length === 0 && <div className="empty-state"><span>—</span><p>今天没有已确认的事件</p></div>}
          {loading && <div className="source-loading"><RefreshCw className="spin" size={14} />正在读取事件…</div>}
        </div>
      </article>

      <article className="panel week-panel">
        <div className="panel-head"><div><h2>未来 7 天重点</h2><p>{loading || error ? "—" : `${importantWeek.length} 个高影响事件`}</p></div><Link href="/week">查看本周 <ChevronRight size={15} /></Link></div>
        <div className="week-list">
          {!loading && !error && importantWeek.map((event) => <WeekItem key={event.id} event={event} timezone={settings.timezone} />)}
          {!loading && !error && importantWeek.length === 0 && <div className="empty-state"><span>—</span><p>未来 7 天暂无高影响事件</p></div>}
        </div>
      </article>
    </section>

    <section className="bottom">
      <article className="panel changes">
        <div className="panel-head"><div><h2>最近变更</h2><p>来自真实版本记录</p></div><Link href="/changes">查看全部 <ChevronRight size={15} /></Link></div>
        {!loading && !error && changes.slice(0, 2).map(({ change, event }) => <div className="change" key={change.id}><span><RefreshCw size={15} /></span><div><h3>{event ? eventTitle(event) : "事件信息已更新"}</h3><p>{changeTypeLabel(change.change_type)} · 版本 {change.to_version}</p></div><time>{relativeTime(change.created_at)}</time></div>)}
        {!loading && !error && changes.length === 0 && <div className="empty-state"><span>—</span><p>暂无变更记录</p></div>}
      </article>
      <article className="panel sources">
        <div className="panel-head"><div><h2>来源健康</h2><p>核心数据源实时状态</p></div><Link href="/sources">详情 <ChevronRight size={15} /></Link></div>
        <SourceHealthSummary />
      </article>
    </section>
  </>;
}

function NextEventCard({ event, loading, timezone }:{ event:ApiEvent|null; loading:boolean; timezone:string }) {
  const dateRange = event
    ? formatDateRange(event.date_range_start ?? event.local_date, event.date_range_end)
    : "";
  const hasDateRange = Boolean(event?.date_range_start && event?.date_range_end);
  return <article className="next-card">
    <div className="next-label"><span><i />下一个关键事件</span>{event && <b>CRITICAL</b>}</div>
    {loading ? <div className="source-loading"><RefreshCw className="spin" size={14} />正在核验下一个关键事件…</div> : event ? <>
      <div className="next-body"><div><small><b>{event.country_code}</b> {eventInstitution(event)} · {event.category}</small><h2>{eventTitle(event)}</h2></div>{event.starts_at ? <Countdown target={event.starts_at} /> : <div className="date-only-badge">{hasDateRange ? "日期范围" : "仅确认日期"}<br />{dateRange}<br />具体时间待定</div>}</div>
      <footer><span><CalendarDays size={15} />{event.starts_at ? formatEventDateTime(event.starts_at, timezone) : dateRange}</span><span><ShieldCheck size={15} />{eventInstitution(event)} · {labels.status[normalizeStatus(event.status)]}</span></footer>
    </> : <div className="empty-state"><span>—</span><p>没有可用的关键事件；请检查数据源状态</p></div>}
  </article>;
}

function Countdown({ target }:{ target:string }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);
  const remaining = Math.max(0, Date.parse(target) - now);
  const days = Math.floor(remaining / 86_400_000);
  const hours = Math.floor((remaining % 86_400_000) / 3_600_000);
  const minutes = Math.floor((remaining % 3_600_000) / 60_000);
  return <div className="countdown"><span><b>{String(days).padStart(2, "0")}</b><small>天</small></span><i>:</i><span><b>{String(hours).padStart(2, "0")}</b><small>小时</small></span><i>:</i><span><b>{String(minutes).padStart(2, "0")}</b><small>分钟</small></span></div>;
}

function DashboardEvent({ event, timezone }:{ event:ApiEvent; timezone:string }) {
  const dateRange = formatDateRange(event.date_range_start ?? event.local_date, event.date_range_end);
  const sourceTimezone = event.original_timezone ? timezoneLabels[event.original_timezone] ?? event.original_timezone : "当地日期";
  return <div className={`event ${!isUpcoming(event) ? "passed" : ""}`}><div className="time"><b>{event.starts_at ? formatEventTime(event.starts_at, timezone) : dateRange}</b><small>{event.starts_at ? eventOriginalTime(event) : `具体时间待定 · ${sourceTimezone}`}</small></div><i className={event.importance === "high" || event.importance === "critical" ? "high-dot" : ""} /><div className="event-copy"><small>{event.country_code} · {eventInstitution(event)}</small><h3>{eventTitle(event)}</h3></div><div className="event-state"><b className={event.importance === "critical" || event.importance === "high" ? "high" : "medium"}>{labels.importance[event.importance]}</b><small>{labels.status[normalizeStatus(event.status)]}</small></div></div>;
}

function WeekItem({ event, timezone }:{ event:ApiEvent; timezone:string }) {
  const date = event.starts_at ? dateKeyInTimezone(new Date(event.starts_at), timezone) : event.local_date ?? "";
  const day = date.slice(-2);
  const weekday = date ? new Intl.DateTimeFormat("zh-CN", { weekday:"short", timeZone:"UTC" }).format(new Date(`${date}T12:00:00Z`)) : "";
  const dateRange = formatDateRange(event.date_range_start ?? event.local_date, event.date_range_end);
  const sourceTimezone = event.original_timezone ? timezoneLabels[event.original_timezone] ?? event.original_timezone : "当地日期";
  return <div className="week-item"><div className="date"><b>{day}</b><small>{weekday}</small></div><div><h3>{eventTitle(event)}</h3><p>{event.starts_at ? `${formatEventTime(event.starts_at, timezone)} · 当前时区` : `${dateRange} · ${sourceTimezone} · 时间待定`}</p></div><i className={event.importance === "critical" ? "critical" : ""} /></div>;
}

function isUpcoming(event:ApiEvent):boolean {
  if (event.status === "cancelled" || event.status === "completed" || event.status === "ignored") return false;
  return event.starts_at ? Date.parse(event.starts_at) >= Date.now() : Boolean(event.local_date);
}

function compareEventTime(left:ApiEvent, right:ApiEvent):number {
  const value = (event:ApiEvent) => Date.parse(event.starts_at ?? `${event.local_date ?? "9999-12-31"}T23:59:59Z`);
  return value(left) - value(right);
}

function normalizeStatus(status:ApiEvent["status"]):"confirmed"|"tba"|"expected"|"rescheduled"|"cancelled"|"ignored" {
  if (status === "provisional" || status === "completed") return "confirmed";
  return status;
}

function changeTypeLabel(type:string):string {
  return ({ created:"新增", rescheduled:"改期", time_confirmed:"时间确认", cancelled:"取消", importance_changed:"重要性变更", updated:"信息更新" } as Record<string,string>)[type] ?? "信息更新";
}

function relativeTime(value:string):string {
  const minutes = Math.max(0, Math.round((Date.now() - Date.parse(value)) / 60_000));
  if (minutes < 60) return `${Math.max(1, minutes)} 分钟前`;
  if (minutes < 1_440) return `${Math.round(minutes / 60)} 小时前`;
  return `${Math.round(minutes / 1_440)} 天前`;
}
