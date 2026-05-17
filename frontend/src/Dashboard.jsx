import React, { useState, useEffect } from 'react';
import { 
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip as RechartsTooltip, ResponsiveContainer, 
  PieChart, Pie, Cell, Legend
} from 'recharts';
import { Activity, Database, Clock, DollarSign, AlertCircle, CheckCircle2 } from 'lucide-react';

// Replace with your API Gateway URL once deployed
const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:3000/api/mock';

export default function Dashboard() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    const fetchData = async () => {
      try {
        const response = await fetch(API_URL);
        if (!response.ok) {
          throw new Error('Network response was not ok');
        }
        const result = await response.json();
        setData(result);
      } catch (err) {
        console.error('Fetch error:', err);
        // Fallback to mock data for demonstration
        setData({
          lastUpdated: new Date().toISOString(),
          kpis: {
            totalVolumeBytes: 38.3 * 1024 * 1024,
            totalBronzeFiles: 5,
            maxFreshnessLag: 17.0,
            lambdaSuccessRate: 100,
            mtdCost: 12.45
          },
          instruments: [
            { id: "BC-MA200", name: "BLACK CARBON MA200", status: "OK", bronzeFiles: 1, silverFiles: 1, syncStatus: 100, freshnessLag: 0.2, bronzeSize: 17.4 * 1024 * 1024 },
            { id: "CO2-LICOR", name: "CO2 LI-COR", status: "DEGRADED", bronzeFiles: 1, silverFiles: 0, syncStatus: 0, freshnessLag: 1.5, bronzeSize: 9.1 * 1024 * 1024 },
            { id: "NEPH-PM25", name: "NEPHELOMETER PM25", status: "ERROR", bronzeFiles: 1, silverFiles: 1, syncStatus: 100, freshnessLag: 17.0, bronzeSize: 0.8 * 1024 * 1024 },
            { id: "NO2-CAPS", name: "NO2 CAPS", status: "OK", bronzeFiles: 1, silverFiles: 1, syncStatus: 100, freshnessLag: 0.1, bronzeSize: 1.5 * 1024 * 1024 },
            { id: "SMPS", name: "SMPS", status: "OK", bronzeFiles: 1, silverFiles: 1, syncStatus: 100, freshnessLag: 0.3, bronzeSize: 9.5 * 1024 * 1024 },
          ],
          trends: {
            dates: ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            cost: [1.2, 1.5, 1.3, 1.8, 1.4, 1.6, 2.0]
          }
        });
      } finally {
        setLoading(false);
      }
    };

    fetchData();
    const interval = setInterval(fetchData, 60000); // Refresh every minute
    return () => clearInterval(interval);
  }, []);

  if (loading) {
    return <div className="flex items-center justify-center min-h-screen text-gray-400">LOADING PIPELINE METRICS...</div>;
  }

  if (!data) return null;

  const { kpis, instruments, trends } = data;
  
  const formatBytes = (bytes) => {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
  };

  const trendData = trends.dates.map((date, i) => ({
    name: date,
    cost: trends.cost[i]
  }));

  const COLORS = {
    cyan: '#00d4ff',
    pink: '#ff2a6d',
    green: '#00ff88',
    darkCard: '#0a0a0a',
    border: '#1a1a1a',
    textMuted: '#666'
  };

  const getStatusColor = (status) => {
    if (status === 'OK') return COLORS.cyan;
    if (status === 'DEGRADED') return '#fbbf24'; // amber
    return COLORS.pink;
  };

  // Cost breakdown pie data
  const pieData = [
    { name: 'Lambda', value: 4.5 },
    { name: 'S3 Storage', value: 2.1 },
    { name: 'API Gateway', value: 0.8 },
    { name: 'Other', value: 5.05 },
  ];
  const pieColors = ['#00d4ff', '#ff2a6d', '#00ff88', '#fbbf24'];

  return (
    <div className="max-w-7xl mx-auto">
      {/* Header */}
      <header className="flex justify-between items-baseline border-b border-gray-800 pb-3 mb-8">
        <h1 className="text-xl font-black tracking-wider text-white">
          DES MOINES: <span className="text-gray-500">PIPELINE MONITOR <span className="text-cyan-neon">v2.0</span></span>
        </h1>
        <div className="text-xs font-semibold text-gray-500 tracking-wider">
          LAST UPDATED: <span className="text-white">{new Date(data.lastUpdated).toLocaleTimeString('en-US', { timeZone: 'UTC' })} UTC</span>
        </div>
      </header>

      {/* KPIs */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-5 mb-8">
        <KPICard title="TOTAL BRONZE VOLUME" value={formatBytes(kpis.totalVolumeBytes)} unit="S3 STORAGE" color="cyan-neon" Icon={Database} />
        <KPICard title="MAX FRESHNESS LAG" value={`${kpis.maxFreshnessLag}h`} unit="HOURS DELAY" color="pink-neon" Icon={Clock} />
        <KPICard title="LAMBDA SUCCESS (24H)" value={`${kpis.lambdaSuccessRate}%`} unit="INVOCATIONS" color="green-neon" Icon={Activity} />
        <KPICard title="MTD PIPELINE COST" value={`$${kpis.mtdCost.toFixed(2)}`} unit="AWS BILLING" color="cyan-neon" Icon={DollarSign} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        
        {/* Instrument Inventory */}
        <div className="lg:col-span-2">
          <div className="flex justify-between border-b border-gray-800 pb-2 mb-4">
            <h2 className="text-[0.65rem] font-extrabold text-gray-500 tracking-widest uppercase">INSTRUMENT INVENTORY & STATUS</h2>
          </div>
          <div className="bg-card-dark border-t-2 border-gray-800 p-4 rounded-b-md">
            <table className="w-full text-left border-collapse">
              <thead>
                <tr>
                  <th className="text-[0.65rem] font-extrabold text-gray-500 tracking-widest uppercase py-3 border-b border-gray-800">Instrument</th>
                  <th className="text-[0.65rem] font-extrabold text-gray-500 tracking-widest uppercase py-3 border-b border-gray-800 text-right">Bronze Files</th>
                  <th className="text-[0.65rem] font-extrabold text-gray-500 tracking-widest uppercase py-3 border-b border-gray-800 text-right">Freshness</th>
                  <th className="text-[0.65rem] font-extrabold text-gray-500 tracking-widest uppercase py-3 border-b border-gray-800 text-right">Sync Status</th>
                </tr>
              </thead>
              <tbody>
                {instruments.map(inst => (
                  <tr key={inst.id} className="hover:bg-gray-900 transition-colors">
                    <td className="py-4 border-b border-gray-800">
                      <div className="flex items-center">
                        <span 
                          className="w-2 h-2 rounded-full mr-3" 
                          style={{ backgroundColor: getStatusColor(inst.status), boxShadow: `0 0 5px ${getStatusColor(inst.status)}80` }}
                        ></span>
                        <div>
                          <div className="font-bold text-gray-200 text-sm tracking-wide">{inst.name}</div>
                          <div className="font-mono text-gray-500 text-xs">{inst.id}</div>
                        </div>
                      </div>
                    </td>
                    <td className="py-4 border-b border-gray-800 text-right">
                      <div className="text-cyan-neon font-black">{inst.bronzeFiles}</div>
                      <div className="text-gray-500 text-[0.6rem] tracking-wider uppercase">{formatBytes(inst.bronzeSize)}</div>
                    </td>
                    <td className="py-4 border-b border-gray-800 text-right">
                      <div className="font-bold text-gray-300">{inst.freshnessLag.toFixed(1)}h</div>
                    </td>
                    <td className="py-4 border-b border-gray-800 text-right">
                      <div className="flex items-center justify-end">
                        <span className="font-bold text-gray-300 mr-2">{inst.syncStatus}%</span>
                        {inst.syncStatus === 100 ? (
                          <CheckCircle2 size={16} className="text-green-neon" />
                        ) : (
                          <AlertCircle size={16} className="text-pink-neon" />
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {/* Charts Column */}
        <div className="space-y-6">
          {/* Cost Trend */}
          <div>
            <div className="flex justify-between border-b border-gray-800 pb-2 mb-4">
              <h2 className="text-[0.65rem] font-extrabold text-gray-500 tracking-widest uppercase">DAILY COST TREND (7D)</h2>
            </div>
            <div className="bg-card-dark border-t-2 border-gray-800 p-4 rounded-b-md h-64">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={trendData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#111" vertical={false} />
                  <XAxis dataKey="name" stroke="#333" tick={{fill: '#666', fontSize: 10}} tickLine={false} axisLine={false} />
                  <YAxis stroke="#333" tick={{fill: '#666', fontSize: 10}} tickLine={false} axisLine={false} tickFormatter={(val) => `$${val}`} />
                  <RechartsTooltip 
                    contentStyle={{ backgroundColor: '#111', borderColor: '#222', borderRadius: '4px', fontSize: '12px' }}
                    itemStyle={{ color: COLORS.cyan }}
                  />
                  <Line type="monotone" dataKey="cost" stroke={COLORS.cyan} strokeWidth={2} dot={{ fill: '#0a0a0a', stroke: COLORS.cyan, strokeWidth: 2 }} activeDot={{ r: 6 }} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>

          {/* Cost Breakdown */}
          <div>
             <div className="flex justify-between border-b border-gray-800 pb-2 mb-4">
              <h2 className="text-[0.65rem] font-extrabold text-gray-500 tracking-widest uppercase">COST BREAKDOWN</h2>
            </div>
            <div className="bg-card-dark border-t-2 border-gray-800 p-4 rounded-b-md h-64 flex flex-col justify-center">
               <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie
                    data={pieData}
                    cx="50%"
                    cy="45%"
                    innerRadius={50}
                    outerRadius={70}
                    paddingAngle={2}
                    dataKey="value"
                    stroke="none"
                  >
                    {pieData.map((entry, index) => (
                      <Cell key={`cell-${index}`} fill={pieColors[index % pieColors.length]} />
                    ))}
                  </Pie>
                  <RechartsTooltip 
                    contentStyle={{ backgroundColor: '#111', borderColor: '#222', borderRadius: '4px', fontSize: '12px' }}
                    itemStyle={{ color: '#ccc' }}
                    formatter={(value) => `$${value.toFixed(2)}`}
                  />
                  <Legend 
                    verticalAlign="bottom" 
                    height={36} 
                    iconType="circle" 
                    wrapperStyle={{ fontSize: '10px', color: '#666' }}
                  />
                </PieChart>
              </ResponsiveContainer>
            </div>
          </div>

        </div>
      </div>
    </div>
  );
}

function KPICard({ title, value, unit, color, Icon }) {
  const colorClass = `text-${color}`;
  
  return (
    <div className="bg-card-dark border-t-2 border-gray-800 p-6 flex flex-col relative rounded-b-md">
      <div className="absolute top-6 right-6 text-gray-800">
        <Icon size={24} />
      </div>
      <div className="text-[0.65rem] font-extrabold text-gray-500 tracking-widest uppercase mb-3">{title}</div>
      <div className={`text-4xl font-black tracking-tight leading-none ${colorClass}`}>{value}</div>
      <div className="text-[0.70rem] font-bold text-gray-600 tracking-wider uppercase mt-2">{unit}</div>
    </div>
  );
}
