require('dotenv').config();

const express = require('express');
const { Pool } = require('pg');
const cors = require('cors');
const { spawn } = require('child_process');
const path = require('path');

const app = express();
app.use(cors());
app.use(express.json());
app.use(express.static('public'));

const pool = new Pool({
  host:     process.env.DB_HOST     || 'localhost',
  port:     parseInt(process.env.DB_PORT) || 5432,
  database: process.env.DB_NAME     || 'indoor',
  user:     process.env.DB_USER     || 'postgres',
  password: process.env.DB_PASS     || 'postgres',
  ssl:      process.env.DB_HOST ? { rejectUnauthorized: false } : false
});

const FLOOR_OFFSET = { '111': 0, '112': 0 };

function inferFeatureType(tableName) {
  if (tableName.startsWith('unit'))    return 'unit';
  if (tableName.startsWith('fixture')) return 'fixture';
  if (tableName.startsWith('level'))   return 'level';
  return null;
}

function getHeight(feature_ty, category, floorSuffix) {
  const offset = FLOOR_OFFSET[floorSuffix] ?? 0;
  if (feature_ty === 'level') return { base: offset, height: offset + 0.2 };
  if (feature_ty === 'unit') {
    const heights = {
      room:      { base: 0.5, height: 3.0 },
      structure: { base: 0.5, height: 3.5 },
      column:    { base: 0.5, height: 3.5 },
      stairs:    { base: 0.5, height: 3.0 },
      steps:     { base: 0.5, height: 0.8 },
      elevator:  { base: 0.5, height: 3.5 },
      restroom:  { base: 0.5, height: 3.0 },
      ramp:      { base: 0.5, height: 0.7 },
    };
    const h = heights[category] || { base: 0.5, height: 2.5 };
    return { base: offset + h.base, height: offset + h.height };
  }
  if (feature_ty === 'fixture') {
    const heights = {
      wall:      { base: 0.5, height: 3.0 },
      furniture: { base: 0.5, height: 1.2 },
      desk:      { base: 0.5, height: 1.2 },
    };
    const h = heights[category] || { base: 0.5, height: 1.0 };
    return { base: offset + h.base, height: offset + h.height };
  }
  return { base: offset, height: offset + 1 };
}

const ALLOWED_TABLES = [
  'unit_111', 'fixture_111', 'level_111',
  'unit_112', 'fixture_112', 'level_112',
];

// ── Indoor GeoJSON ─────────────────────────────────────────
app.get('/api/indoor/:tabel', async (req, res) => {
  const tabel = req.params.tabel;
  if (!ALLOWED_TABLES.includes(tabel)) {
    return res.status(400).json({ error: 'Tabel tidak diizinkan' });
  }
  const floorSuffix = tabel.split('_').pop();
  const feature_ty  = inferFeatureType(tabel);
  const isLevel     = feature_ty === 'level';
  const query = isLevel
    ? `SELECT gid, fid, ST_AsGeoJSON(ST_Transform(geom, 4326))::json AS geometry FROM ${tabel}`
    : `SELECT gid, category, fid, ST_AsGeoJSON(ST_Transform(geom, 4326))::json AS geometry FROM ${tabel}`;
  try {
    const result = await pool.query(query);
    const geojson = {
      type: 'FeatureCollection',
      features: result.rows.map(row => ({
        type: 'Feature',
        geometry: row.geometry,
        properties: {
          gid: row.gid,
          feature_ty,
          category: row.category ?? null,
          id: row.fid,
          floor: floorSuffix,
          ...getHeight(feature_ty, row.category, floorSuffix)
        }
      }))
    };
    res.json(geojson);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: err.message });
  }
});

// ── GET POI per lantai ─────────────────────────────────────
// PERUBAHAN: endpoint sekarang /api/poi/:floor
// Sebelumnya /api/poi hardcode ke node_111
// Sekarang /api/poi/111 → node_111, /api/poi/112 → node_112
app.get('/api/poi/:floor', async (req, res) => {
  const floor = req.params.floor;
  if (!['111', '112'].includes(floor)) {
    return res.status(400).json({ error: 'Floor tidak valid, gunakan 111 atau 112' });
  }
  const table = `node_${floor}`;
  try {
    const result = await pool.query(`
      SELECT id, nama, jenis,
        ST_X(geom) as lng,
        ST_Y(geom) as lat
      FROM ${table}
      ORDER BY id
    `);
    res.json(result.rows);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: err.message });
  }
});

// ── GET routing ────────────────────────────────────────────
// PERUBAHAN: tambah parameter floor yang diteruskan ke routing.py
// Sebelumnya tidak ada floor, hardcode ke node_111/network_111
app.get('/api/route', async (req, res) => {
  const { from, to, floor = '111' } = req.query;
  if (!from || !to) {
    return res.status(400).json({ error: 'Parameter from dan to wajib diisi' });
  }
  if (!['111', '112'].includes(floor)) {
    return res.status(400).json({ error: 'Floor tidak valid' });
  }

  const scriptPath = path.join(__dirname, 'routing.py');
  const py = spawn('python3', [
    scriptPath,
    '--from',  from,
    '--to',    to,
    '--floor', floor
  ]);

  let result = '';
  let errMsg = '';

  py.stdout.on('data', (data) => result += data.toString());
  py.stderr.on('data', (data) => errMsg += data.toString());

  py.on('error', (err) => {
    console.error('Spawn error:', err);

    return res.status(500).json({
      error: 'Gagal menjalankan Python',
      detail: err.message
    });
  });

  py.on('close', (code) => {
    if (code !== 0) {
      console.error('Python error:', errMsg);
      return res.status(500).json({ error: 'Routing gagal', detail: errMsg });
    }
    try {
      const parsed = JSON.parse(result);
      if (parsed.error) return res.status(400).json(parsed);
      res.json(parsed);
    } catch (e) {
      res.status(500).json({ error: 'Output Python tidak valid JSON', raw: result });
    }
  });
});

app.get('/api/debug', (req, res) => {
  const { spawn } = require('child_process');
  const py = spawn('python3', ['--version']);
  let out = '', err = '';
  py.stdout.on('data', d => out += d);
  py.stderr.on('data', d => err += d);
  py.on('close', code => {
    res.json({ code, out, err, 
      cwd: process.cwd(),
      files: require('fs').readdirSync('.') 
    });
  });
});

app.get('/api/debug2', (req, res) => {
  const { spawn } = require('child_process');
  const py = spawn('python3', ['-c', 'import geopandas; import networkx; import pyproj; import sqlalchemy; print("OK")']);
  let out = '', err = '';
  py.stdout.on('data', d => out += d);
  py.stderr.on('data', d => err += d);
  py.on('close', code => res.json({ code, out, err }));
});

app.listen(3000, () => {
  console.log('Server jalan di http://localhost:3000');
});
