// ============================================================
// INVENTARIO SALE SERVER — livello dati
// In produzione: sostituire questo modulo con chiamate API
// (GET /inventario, PUT /inventario) verso il vostro backend/CMDB.
//
// Struttura: locations[] → sale[] → vani[] (stanze fisiche) + racks[]
// Coordinate in metri, origine in alto a sinistra del riquadro sala.
// ============================================================

const D = (name, type, model, ip, serial, owner, u, h) =>
  ({ id: name, name, type, model, ip, serial, owner, u, h: h || 1 });

const R = (id, name, row, x, y, w, h, u, devices) =>
  ({ id, name, row, u: u || 45, x, y, w, h, devices: devices || [] });

// ---- Frontend: due isole di rack ----
const frontendRacks = [];
for (let i = 0; i < 10; i++) frontendRacks.push(R('FE-R' + String(i + 1).padStart(2, '0'), 'Rack FE-R' + String(i + 1).padStart(2, '0'), 'A', 1.00, 0.30 + i * 0.70, 0.68, 0.66));
for (let i = 0; i < 9; i++) frontendRacks.push(R('FE-R' + String(i + 11).padStart(2, '0'), 'Rack FE-R' + String(i + 11).padStart(2, '0'), 'B', 3.85, 0.80 + i * 0.55, 0.62, 0.53));
frontendRacks.push(R('FE-Q01', 'Armadio tecnico', '—', 3.00, 0.05, 0.80, 0.55, 12));

// ---- Saletta Nuova: vano A (piccolo, in alto) + vano B (lungo) ----
const salettaRacks = [
  R('SN-R01', 'Rack SN-R01', 'A', 3.55, 0.35, 0.60, 0.62),
  R('SN-R02', 'Rack SN-R02', 'A', 3.55, 1.15, 0.60, 0.62),
  R('SN-R03', 'Rack SN-R03', 'A', 3.55, 1.95, 0.60, 0.62),
  R('SN-R04', 'Rack SN-R04', 'A', 3.55, 2.75, 0.60, 0.62),
  R('SN-R05', 'Rack SN-R05', 'A', 0.45, 1.30, 0.60, 0.62),
  R('SN-R06', 'Rack SN-R06', 'A', 0.45, 2.50, 0.60, 0.62)
];
for (let i = 0; i < 10; i++) salettaRacks.push(R('SN-R' + String(i + 7).padStart(2, '0'), 'Rack SN-R' + String(i + 7).padStart(2, '0'), 'B', 0.65, 4.40 + i * 0.66, 0.68, 0.63));
for (let i = 0; i < 6; i++) salettaRacks.push(R('SN-R' + String(i + 17).padStart(2, '0'), 'Rack SN-R' + String(i + 17).padStart(2, '0'), 'C', 3.05, 4.40 + i * 0.63, 0.62, 0.60));
salettaRacks.push(R('SN-R23', 'Rack SN-R23', 'B', 0.70, 11.85, 0.62, 0.62));
salettaRacks.push(R('SN-R24', 'Rack SN-R24', 'C', 3.00, 11.85, 0.60, 0.60));

export const DATI = {
  versione: 3,
  // utenza di bootstrap per il collaudo — rimuovere in produzione
  utenti: [{ email: 'admin', ruolo: 'admin', password: 'admin' }],
  locations: [
    {
      id: 'pomezia-g0',
      nome: 'Pomezia — G0',
      sale: [
        {
          id: 'backend', nome: 'Backend', w: 4.25, h: 4.99, area: '21.18 m²', dim: '4.25 × 4.99 m',
          vani: [{ x: 0, y: 0, w: 4.25, h: 4.99, porta: { lato: 'bottom', x: 0.35, w: 0.84 } }],
          racks: [
            { id: 'R01', name: 'Rack R01 — Core', row: 'A', u: 45, x: 0.05, y: 0.75, w: 0.60, h: 0.85, devices: [
              D('fw-01', 'firewall', 'FortiGate 200F', '10.0.0.1', 'FG2-8841', 'Team Rete', 40),
              D('sw-core-01', 'rete', 'Cisco C9300-48T', '10.0.0.11', 'FCW2231', 'Team Rete', 38),
              D('sw-core-02', 'rete', 'Cisco C9300-48T', '10.0.0.12', 'FCW2232', 'Team Rete', 37),
              D('srv-web-01', 'server', 'Dell R650', '10.0.1.21', 'SN-7HQ2K', 'Team Infra', 30),
              D('srv-web-02', 'server', 'Dell R650', '10.0.1.22', 'SN-7HQ3L', 'Team Infra', 28)
            ]},
            { id: 'R02', name: 'Rack R02 — Database', row: 'A', u: 45, x: 0.05, y: 1.63, w: 0.60, h: 0.85, devices: [
              D('srv-db-01', 'server', 'Dell R750', '10.0.2.31', 'SN-9DK1M', 'DBA', 32, 2),
              D('srv-db-02', 'server', 'Dell R750', '10.0.2.32', 'SN-9DK2N', 'DBA', 29, 2),
              D('nas-01', 'storage', 'Synology RS3621xs+', '10.0.2.40', 'SYN-2210', 'Team Infra', 20, 2)
            ]},
            { id: 'R03', name: 'Rack R03 — Virtualizzazione', row: 'A', u: 45, x: 0.05, y: 2.51, w: 0.60, h: 0.85, devices: [
              D('srv-vm-01', 'server', 'HPE DL380 Gen11', '10.0.3.51', 'CZJ1201', 'Team Infra', 30, 2),
              D('srv-vm-02', 'server', 'HPE DL380 Gen11', '10.0.3.52', 'CZJ1202', 'Team Infra', 27, 2),
              D('srv-vm-03', 'server', 'HPE DL380 Gen11', '10.0.3.53', 'CZJ1203', 'Team Infra', 24, 2)
            ]},
            { id: 'UPS', name: 'Armadio UPS', row: 'A', u: 12, x: 0.05, y: 3.55, w: 0.80, h: 0.62, devices: [
              D('ups-01', 'alimentazione', 'APC Smart-UPS SRT 5000', '10.0.0.90', 'AS1948', 'Team Infra', 1, 6)
            ]},
            { id: 'R04', name: 'Rack R04 — Applicativi', row: 'B', u: 45, x: 3.60, y: 0.95, w: 0.60, h: 0.60, devices: [
              D('sw-tor-04', 'rete', 'Cisco C9200-24T', '10.0.0.14', 'FCW2404', 'Team Rete', 42),
              D('srv-app-01', 'server', 'Dell R650', '10.0.4.61', 'SN-4AP1Q', 'Team Dev', 30),
              D('srv-app-02', 'server', 'Dell R650', '10.0.4.62', 'SN-4AP2R', 'Team Dev', 28)
            ]},
            { id: 'R05', name: 'Rack R05 — Storage SAN', row: 'B', u: 45, x: 3.60, y: 1.605, w: 0.60, h: 0.60, devices: [
              D('sw-tor-05', 'rete', 'Cisco C9200-24T', '10.0.0.15', 'FCW2405', 'Team Rete', 42),
              D('san-01', 'storage', 'Dell ME5024', '10.0.5.71', 'ME5-3301', 'Team Infra', 20, 2),
              D('san-02', 'storage', 'Dell ME5024', '10.0.5.72', 'ME5-3302', 'Team Infra', 18, 2)
            ]},
            { id: 'R06', name: 'Rack R06 — Backup', row: 'B', u: 45, x: 3.60, y: 2.26, w: 0.60, h: 0.60, devices: [
              D('sw-tor-06', 'rete', 'Cisco C9200-24T', '10.0.0.16', 'FCW2406', 'Team Rete', 42),
              D('srv-bck-01', 'server', 'Dell R750', '10.0.6.81', 'SN-6BK1S', 'Team Infra', 25, 2),
              D('lib-01', 'storage', 'IBM TS4300', '10.0.6.85', 'TS4-0912', 'Team Infra', 10, 3)
            ]},
            { id: 'R07', name: 'Rack R07 — Kubernetes', row: 'B', u: 45, x: 3.60, y: 2.915, w: 0.60, h: 0.60, devices: [
              D('sw-tor-07', 'rete', 'Cisco C9200-24T', '10.0.0.17', 'FCW2407', 'Team Rete', 42),
              D('srv-k8s-01', 'server', 'HPE DL360 Gen11', '10.0.7.91', 'CZJ1301', 'Team Dev', 30),
              D('srv-k8s-02', 'server', 'HPE DL360 Gen11', '10.0.7.92', 'CZJ1302', 'Team Dev', 28),
              D('srv-k8s-03', 'server', 'HPE DL360 Gen11', '10.0.7.93', 'CZJ1303', 'Team Dev', 26)
            ]},
            { id: 'R08', name: 'Rack R08 — GPU', row: 'B', u: 45, x: 3.60, y: 3.57, w: 0.60, h: 0.60, devices: [
              D('sw-tor-08', 'rete', 'Cisco C9200-24T', '10.0.0.18', 'FCW2408', 'Team Rete', 42),
              D('srv-gpu-01', 'server', 'Supermicro SYS-421GE', '10.0.8.95', 'SM-GP41', 'Team Dev', 20, 4)
            ]},
            { id: 'R09', name: 'Rack R09 — Test', row: 'B', u: 45, x: 3.60, y: 4.225, w: 0.60, h: 0.60, devices: [
              D('sw-tor-09', 'rete', 'Cisco C9200-24T', '10.0.0.19', 'FCW2409', 'Team Rete', 42),
              D('srv-test-01', 'server', 'Dell R650', '10.0.9.97', 'SN-9TS1T', 'Team Dev', 30)
            ]}
          ]
        },
        {
          id: 'centro-stella', nome: 'Centro Stella', w: 2.79, h: 4.97, area: '13.85 m²', dim: '2.79 × 4.97 m',
          vani: [{ x: 0, y: 0, w: 2.79, h: 4.97, porta: { lato: 'bottom', x: 1.77, w: 0.87 } }],
          racks: [
            R('CS-R01', 'Rack CS-R01', 'A', 0.45, 0.80, 0.62, 0.55),
            R('CS-R02', 'Rack CS-R02', 'A', 0.45, 1.40, 0.62, 0.55),
            R('CS-R03', 'Rack CS-R03', 'A', 0.45, 2.00, 0.62, 0.55),
            R('CS-R04', 'Rack CS-R04', 'A', 0.45, 2.60, 0.62, 0.55),
            R('CS-R05', 'Rack CS-R05', 'A', 0.45, 3.20, 0.62, 0.55),
            R('CS-R06', 'Rack CS-R06', 'A', 1.10, 0.80, 0.62, 0.55),
            R('CS-Q01', 'Quadro elettrico', '—', 2.15, 2.70, 0.42, 0.42, 6)
          ]
        },
        {
          id: 'frontend', nome: 'Frontend', w: 4.89, h: 7.50, area: '36.63 m²', dim: '4.89 × 7.50 m',
          vani: [{ x: 0, y: 0, w: 4.89, h: 7.50, porta: { lato: 'bottom', x: 3.85, w: 0.90 } }],
          racks: frontendRacks
        },
        {
          id: 'saletta-nuova', nome: 'Saletta Nuova', w: 4.23, h: 14.20, area: '57.19 m² (2 vani)', dim: '4.23 × 3.65 m + 4.23 × 10.43 m',
          vani: [
            { x: 0, y: 0, w: 4.23, h: 3.65, porta: { lato: 'bottom', x: 2.20, w: 0.90 } },
            { x: 0, y: 3.77, w: 4.23, h: 10.43, porta: { lato: 'right', y: 6.55, w: 0.96 } }
          ],
          racks: salettaRacks
        }
      ]
    },
    {
      id: 'pomezia-h0',
      nome: 'Pomezia — H0',
      sale: [
        {
          id: 'h0', nome: 'H0', w: 7.58, h: 8.18, area: '61.68 m²', dim: '7.58 × 8.18 m',
          vani: [{ x: 0, y: 0, w: 7.58, h: 8.18, porta: { lato: 'left', y: 0.71, w: 0.80 }, porta2: { lato: 'bottom', x: 4.30, w: 3.00 } }],
          racks: (() => {
            const rs = [];
            const rows = [ ['A', 1.95], ['B', 3.75], ['C', 5.00] ];
            let n = 1;
            for (const [row, x] of rows) {
              for (let i = 0; i < 8; i++) {
                rs.push(R('H0-R' + String(n).padStart(2, '0'), 'Rack H0-R' + String(n).padStart(2, '0'), row, x, 2.35 + i * 0.66, 0.50, 0.64));
                n++;
              }
            }
            rs.push(R('H0-Q01', 'Armadio a muro', '—', 6.95, 2.30, 0.45, 1.90, 12));
            return rs;
          })()
        }
      ]
    }
    // Prossime location: aggiungere qui
    ,{
      id: 'oriolo-romano',
      nome: 'Oriolo Romano',
      sale: [
        {
          id: 'sala-oriolo', nome: 'Sala Oriolo', w: 9.2, h: 6.0, area: '55.20 m²', dim: '9.20 × 6.00 m',
          segnaposto: false,
          vani: [{ x: 0, y: 0, w: 9.2, h: 6.0, porta: { lato: 'left', y: 4.9, w: 0.9 } }],
          racks: (() => {
            // seriali = 1 o 2 numeri di serie per rack (matching asset)
            const RK = (id, seriali, i, riga, devNames) => {
              let u = 44;
              const devices = devNames.map(([name, type, h]) => {
                const d = D(name, type, '', '', '', '', Math.max(1, u - ((h || 1) - 1)), h || 1);
                u -= (h || 1) + 1;
                return d;
              });
              return { id, name: 'Rack ' + id, row: riga, u: 45,
                x: 0.30 + i * 0.63, y: riga === 'A' ? 0.15 : 5.10, w: 0.63, h: 0.72,
                seriali, devices };
            };
            return [
              RK('RO-A01', ['2020047937'], 0, 'A', [['DR-ESX1','server'],['DR-ESX2','server']]),
              RK('RO-A02', ['2020053938'], 1, 'A', [['DELL EMC UNITY XT','storage',4]]),
              RK('RO-A03', ['2020044486'], 2, 'A', [['ISILON','storage',4]]),
              RK('RO-A04', ['2020044485'], 3, 'A', [['VNX','storage',4]]),
              RK('RO-A05', ['2006029486'], 4, 'A', [['DR-ESX04','server'],['CISCO UCS','server'],['DR-ESX05','server'],['CISCO 4402','rete']]),
              RK('RO4066', ['2006004084'], 5, 'A', [['ALTEON 5208','rete'],['FORTIGATE 800C 1','firewall'],['SWITCH CMCOLL 1','rete'],['FORTIGATE 600E 1','firewall'],['SPIDDRDB1','server'],['SPIDDRFE1','server'],['SPIDDRBE1','server'],['SPIDDRDIR1','server']]),
              RK('RO4037', ['2006004088'], 6, 'A', [['FORTIGATE 800C 2','firewall'],['SWITCH CMCOLL 2','rete'],['ALTEON 5208 2','rete'],['FORTIGATE 600E 2','firewall'],['CISCO ISR4400','rete'],['MEINBERG','altro'],['SPIDDRDB2','server'],['SPIDDRFE2','server'],['SPIDDRBE2','server'],['SPIDDRDIR2','server']]),
              RK('RO4067', ['2006004090'], 7, 'A', [['DR-PECESXI5','server'],['DR-PECESXI3','server'],['DR-PECHSM1','altro'],['DR-PECPOP1','server']]),
              RK('RO4082', ['2006004091'], 8, 'A', [['CRYPTO-HSM-TEST','altro'],['CAOP-DRFE01','server'],['CRYPTO-HSM1','altro'],['LUNA HSM 1','altro'],['LUNA HSM 2','altro'],['DR-PECESXI2','server'],['DR-PECPOP2','server'],['CONSERVAZIONE LINEA1 A','server'],['CONSERVAZIONE LINEA1 B','server']]),
              RK('R11024', ['2008034260'], 9, 'A', [['DR-ESX03','server'],['DR-PECESXI1','server'],['FA-BE2 (Banca Sella)','server'],['RDBMS','server'],['OPTISERVER-DR','server'],['CONSERVAZIONE LINEA2 A','server'],['CONSERVAZIONE LINEA2 B','server']]),
              RK('RO-RETE', ['2012170418'], 10, 'A', [['SWITCH CISCO C-1 1','rete'],['SWITCH CISCO C-1 2','rete'],['SWITCH CISCO C-1 3','rete'],['ROUTER HYPERWAY','rete'],['SWITCH DRCORE1','rete'],['RADWARE 4208 1','rete'],['RADWARE 4208 2','rete']]),
              RK('R11115', ['2012170515'], 0.5, 'B', []),
              RK('RO4495', ['2004126690', '2020044487'], 1.5, 'B', [['VNX 2','storage',4]]),
              RK('RO-B03', ['2020062936', '2020062935'], 2.5, 'B', [['ISILON 2','storage',4]]),
              RK('RO-B04', ['2006029736'], 8.5, 'B', [['ISILON 3','storage',4]]),
              RK('RO-B05', ['2006004089'], 9.5, 'B', [])
            ];
          })()
        }
      ]
    }
  ]
};
