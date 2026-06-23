import { useState, useEffect } from 'react';
import { Activity, MapPin, Clock, DollarSign, RefreshCw } from 'lucide-react';

const API_URL = import.meta.env.VITE_API_URL || 'https://yvhb48sthk.execute-api.us-west-2.amazonaws.com/metrics';

export default function Dashboard() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    const fetchData = async () => {
      try {
        const response = await fetch(API_URL);
        if (!response.ok) {
          throw new Error(`API returned ${response.status}`);
        }
        const result = await response.json();
        setData(result);
        setError(null);
      } catch (err) {
        console.error('Fetch error:', err);
        setError(err.message || 'API request failed');
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    };
    fetchData();
    const interval = setInterval(fetchData, 60000);
    return () => clearInterval(interval);
  }, []);

  const handleRefresh = async () => {
    setRefreshing(true);
    try {
      const response = await fetch(API_URL);
      if (!response.ok) {
        throw new Error(`API returned ${response.status}`);
      }
      const result = await response.json();
      setData(result);
      setError(null);
    } catch (err) {
      console.error('Fetch error:', err);
      setError(err.message || 'API request failed');
    } finally {
      setRefreshing(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-black text-gray-400 font-mono">
        INITIALIZING SENSORS...
      </div>
    );
  }

  if (!data) {
    return (
      <div className="flex flex-col gap-5 items-center justify-center min-h-screen bg-black text-gray-300 font-mono p-6">
        <div className="text-sm tracking-widest text-red-400">API CONNECTION UNAVAILABLE</div>
        <div className="max-w-xl text-center text-xs text-gray-500">{error || 'No metrics payload returned.'}</div>
        <button
          onClick={handleRefresh}
          disabled={refreshing}
          className="inline-flex items-center gap-2 text-xs font-bold tracking-widest uppercase px-3 py-1.5 rounded border border-cyan-400 text-cyan-400 disabled:opacity-50"
        >
          <RefreshCw size={14} className={refreshing ? 'animate-spin' : ''} />
          Retry
        </button>
      </div>
    );
  }

  const { kpis, instruments, refreshTime, systemStatus } = data;

  const formatBytes = (bytes) => {
    if (!bytes || bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
  };

  const formatSeattleTime = (isoString) => {
    if (!isoString) return "NO DATA";
    return new Date(isoString).toLocaleString('en-US', {
      timeZone: 'America/Los_Angeles',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: true
    });
  };

  return (
    <div
      className="min-h-screen bg-black text-gray-300 font-sans p-6"
      style={{
        backgroundImage: 'linear-gradient(rgba(5, 5, 5, 0.85), rgba(5, 5, 5, 0.95)), url("/bg.png")',
        backgroundSize: 'cover',
        backgroundPosition: 'center',
        backgroundAttachment: 'fixed'
      }}
    >
      <div className="max-w-6xl mx-auto">
        <header className="flex justify-between items-baseline border-b border-gray-800/60 pb-3 mb-8">
          <h1 className="text-xl font-black tracking-wider text-white">
            AQ MONITOR <span className="text-gray-500">| <span className="text-cyan-400">SEATTLE</span></span>
          </h1>
          <div className="flex items-center gap-4">
            <div className="text-xs font-semibold text-gray-500 tracking-wider">
              SYSTEM STATUS: <span className={systemStatus === 'ONLINE' ? 'text-green-400' : 'text-red-400'}>{systemStatus || 'CHECKING...'}</span>
            </div>
            <button
              id="refresh-btn"
              onClick={handleRefresh}
              disabled={refreshing}
              className="text-xs font-bold tracking-widest uppercase px-3 py-1.5 rounded border transition-all"
              style={{
                borderColor: refreshing ? '#374151' : '#22d3ee',
                color: refreshing ? '#6b7280' : '#22d3ee',
                background: 'transparent',
                cursor: refreshing ? 'not-allowed' : 'pointer',
                opacity: refreshing ? 0.5 : 1,
              }}
            >
              <span className="inline-flex items-center gap-2">
                <RefreshCw size={13} className={refreshing ? 'animate-spin' : ''} />
                {refreshing ? 'REFRESHING...' : 'REFRESH'}
              </span>
            </button>
          </div>
        </header>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-5 mb-10">
          <KPICard title="LAST UPLOAD" value={refreshTime ? formatSeattleTime(refreshTime).split(',')[1]?.trim() : 'NO DATA'} unit="SEATTLE TIME" color="text-cyan-400" Icon={Clock} />
          <KPICard title="MTD COST" value={kpis.mtdCost === "N/A" ? "N/A" : `$${kpis.mtdCost}`} unit={kpis.costScope || "AWS ACCOUNT MTD"} color="text-pink-500" Icon={DollarSign} />
          <KPICard title="LATEST UPLOAD" value={kpis.lastUpdatedInstrument} unit="INSTRUMENT" color="text-green-400" Icon={Activity} />
          <KPICard title="SITE NAME" value={kpis.siteName} unit="LOCATION" color="text-cyan-400" Icon={MapPin} />
        </div>

        <div className="bg-black/60 backdrop-blur-sm border border-gray-800/50 rounded-lg p-6 shadow-2xl">
          <div className="flex justify-between border-b border-gray-800/50 pb-3 mb-4">
            <h2 className="text-[0.70rem] font-extrabold text-gray-400 tracking-widest uppercase">INSTRUMENT DATA INVENTORY</h2>
          </div>

          <table className="w-full text-left border-collapse">
            <thead>
              <tr>
                <th className="text-[0.65rem] font-extrabold text-gray-500 tracking-widest uppercase py-3 border-b border-gray-800/50">Instrument</th>
                <th className="text-[0.65rem] font-extrabold text-gray-500 tracking-widest uppercase py-3 border-b border-gray-800/50 text-right">Data Rows</th>
                <th className="text-[0.65rem] font-extrabold text-gray-500 tracking-widest uppercase py-3 border-b border-gray-800/50 text-right">Last Update Time (PST/PDT)</th>
              </tr>
            </thead>
            <tbody>
              {instruments.map(instrument => {
                const isActive = instrument.lastUpdate !== null;

                return (
                  <tr key={instrument.id} className="hover:bg-white/5 transition-colors group">
                    <td className="py-5 border-b border-gray-800/30">
                      <div className="flex items-center">
                        <span className={`w-2 h-2 rounded-full mr-4 ${isActive ? 'bg-cyan-400 shadow-[0_0_8px_#22d3ee]' : 'bg-gray-700'}`}></span>
                        <div>
                          <div className="font-bold text-gray-200 text-sm tracking-wide group-hover:text-cyan-300 transition-colors">{instrument.name}</div>
                          <div className="font-mono text-gray-500 text-[0.65rem]">{instrument.id}</div>
                        </div>
                      </div>
                    </td>
                    <td className="py-5 border-b border-gray-800/30 text-right">
                      <div className="text-white font-bold">{(instrument.bronzeRows || 0).toLocaleString()}</div>
                      <div className="text-gray-500 text-[0.6rem] tracking-wider uppercase">{formatBytes(instrument.bronzeSize)}</div>
                    </td>
                    <td className="py-5 border-b border-gray-800/30 text-right">
                      <div className="font-mono text-sm text-gray-300">{formatSeattleTime(instrument.lastUpdate)}</div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function KPICard({ title, value, unit, color, Icon }) {
  return (
    <div className="bg-black/60 backdrop-blur-sm border border-gray-800/50 p-6 flex flex-col relative rounded-lg shadow-xl hover:border-gray-700 transition-colors">
      <div className="absolute top-6 right-6 text-gray-700">
        <Icon size={20} />
      </div>
      <div className="text-[0.65rem] font-extrabold text-gray-500 tracking-widest uppercase mb-3">{title}</div>
      <div className={`text-3xl font-black tracking-tight leading-none ${color}`}>{value}</div>
      <div className="text-[0.65rem] font-bold text-gray-600 tracking-wider uppercase mt-3">{unit}</div>
    </div>
  );
}
