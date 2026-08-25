// ============================================================
// INVENTARIO SALE SERVER — livello dati (seed)
//
// GENERATO da tools/migrate-seed-uids.mjs — non modificare gli `_uid` a mano.
//
// Ogni location, sala, rack, dispositivo e voce di manuale porta un `_uid`:
// UUID v4 immutabile che è la vera identità dell'entità. I codici (`id`:
// R01, srv-db-01) sono rinominabili e NON sono identità.
// Vedi BACKEND-PLAN.md §8.4 e handoff/identity.js.
//
// I `vani` non hanno `_uid`: sono la geometria della sala, non entità.
//
// `schemaVersion` è la forma del documento (§8.13), da non confondere con la
// revisione ottimistica dell'inventario. Il campo `versione` è un residuo
// informale del prototipo e non ha semantica.
//
// In produzione questo modulo è sostituito dalle chiamate all'API
// (GET /api/inventory) e non entra nell'immagine web (§6 del piano).
//
// Entità con identità: 197 · schemaVersion: 1
// ============================================================

export const DATI = {
  "versione": 3,
  "utenti": [
    {
      "email": "admin",
      "ruolo": "admin",
      "password": "admin"
    }
  ],
  "locations": [
    {
      "_uid": "c731f700-b920-4deb-9886-6b1364bf57b4",
      "id": "pomezia-g0",
      "nome": "Pomezia — G0",
      "sale": [
        {
          "_uid": "5dea9ef5-ea20-4087-af0c-e5d925b7641c",
          "id": "backend",
          "nome": "Backend",
          "w": 4.25,
          "h": 4.99,
          "area": "21.18 m²",
          "dim": "4.25 × 4.99 m",
          "vani": [
            {
              "x": 0,
              "y": 0,
              "w": 4.25,
              "h": 4.99,
              "porta": {
                "lato": "bottom",
                "x": 0.35,
                "w": 0.84
              }
            }
          ],
          "racks": [
            {
              "_uid": "a227d36e-20d5-42b9-9aea-8fed156b9118",
              "id": "R01",
              "name": "Rack R01 — Core",
              "row": "A",
              "u": 45,
              "x": 0.05,
              "y": 0.75,
              "w": 0.6,
              "h": 0.85,
              "devices": [
                {
                  "_uid": "723886e1-4f8a-480c-a52a-7a7f1d865cd8",
                  "id": "fw-01",
                  "name": "fw-01",
                  "type": "firewall",
                  "model": "FortiGate 200F",
                  "ip": "10.0.0.1",
                  "serial": "FG2-8841",
                  "owner": "Team Rete",
                  "u": 40,
                  "h": 1
                },
                {
                  "_uid": "2b9825bf-c4c7-4cb4-8be6-03ec3b023581",
                  "id": "sw-core-01",
                  "name": "sw-core-01",
                  "type": "rete",
                  "model": "Cisco C9300-48T",
                  "ip": "10.0.0.11",
                  "serial": "FCW2231",
                  "owner": "Team Rete",
                  "u": 38,
                  "h": 1
                },
                {
                  "_uid": "1455537b-aa9a-4961-b2e9-300ac9613bc5",
                  "id": "sw-core-02",
                  "name": "sw-core-02",
                  "type": "rete",
                  "model": "Cisco C9300-48T",
                  "ip": "10.0.0.12",
                  "serial": "FCW2232",
                  "owner": "Team Rete",
                  "u": 37,
                  "h": 1
                },
                {
                  "_uid": "0e9f11e5-7a7e-49cb-b491-9f11037219b9",
                  "id": "srv-web-01",
                  "name": "srv-web-01",
                  "type": "server",
                  "model": "Dell R650",
                  "ip": "10.0.1.21",
                  "serial": "SN-7HQ2K",
                  "owner": "Team Infra",
                  "u": 30,
                  "h": 1
                },
                {
                  "_uid": "353bedd3-4ffa-4074-9023-6c02e03c354e",
                  "id": "srv-web-02",
                  "name": "srv-web-02",
                  "type": "server",
                  "model": "Dell R650",
                  "ip": "10.0.1.22",
                  "serial": "SN-7HQ3L",
                  "owner": "Team Infra",
                  "u": 28,
                  "h": 1
                }
              ]
            },
            {
              "_uid": "eafd9d2b-130c-4ec6-aa15-1647bdd98bc1",
              "id": "R02",
              "name": "Rack R02 — Database",
              "row": "A",
              "u": 45,
              "x": 0.05,
              "y": 1.63,
              "w": 0.6,
              "h": 0.85,
              "devices": [
                {
                  "_uid": "4766e880-bbdc-4eda-8cb9-bba7f6615854",
                  "id": "srv-db-01",
                  "name": "srv-db-01",
                  "type": "server",
                  "model": "Dell R750",
                  "ip": "10.0.2.31",
                  "serial": "SN-9DK1M",
                  "owner": "DBA",
                  "u": 32,
                  "h": 2
                },
                {
                  "_uid": "85f83c8d-ddfd-4275-9e55-8ba3634cd034",
                  "id": "srv-db-02",
                  "name": "srv-db-02",
                  "type": "server",
                  "model": "Dell R750",
                  "ip": "10.0.2.32",
                  "serial": "SN-9DK2N",
                  "owner": "DBA",
                  "u": 29,
                  "h": 2
                },
                {
                  "_uid": "dbb3b65f-a104-4260-9701-758b22fe195a",
                  "id": "nas-01",
                  "name": "nas-01",
                  "type": "storage",
                  "model": "Synology RS3621xs+",
                  "ip": "10.0.2.40",
                  "serial": "SYN-2210",
                  "owner": "Team Infra",
                  "u": 20,
                  "h": 2
                }
              ]
            },
            {
              "_uid": "9103a283-6800-44c2-901f-31008f1d644c",
              "id": "R03",
              "name": "Rack R03 — Virtualizzazione",
              "row": "A",
              "u": 45,
              "x": 0.05,
              "y": 2.51,
              "w": 0.6,
              "h": 0.85,
              "devices": [
                {
                  "_uid": "ea58a835-5da8-4c96-b3f3-0b96c5817acb",
                  "id": "srv-vm-01",
                  "name": "srv-vm-01",
                  "type": "server",
                  "model": "HPE DL380 Gen11",
                  "ip": "10.0.3.51",
                  "serial": "CZJ1201",
                  "owner": "Team Infra",
                  "u": 30,
                  "h": 2
                },
                {
                  "_uid": "dfbc423f-ab91-4786-b9e7-5aa02ac0f421",
                  "id": "srv-vm-02",
                  "name": "srv-vm-02",
                  "type": "server",
                  "model": "HPE DL380 Gen11",
                  "ip": "10.0.3.52",
                  "serial": "CZJ1202",
                  "owner": "Team Infra",
                  "u": 27,
                  "h": 2
                },
                {
                  "_uid": "bf4132ed-8e88-4b3b-9248-06246c4925b9",
                  "id": "srv-vm-03",
                  "name": "srv-vm-03",
                  "type": "server",
                  "model": "HPE DL380 Gen11",
                  "ip": "10.0.3.53",
                  "serial": "CZJ1203",
                  "owner": "Team Infra",
                  "u": 24,
                  "h": 2
                }
              ]
            },
            {
              "_uid": "416a84bb-78ba-40bb-adf7-9d34863ddb09",
              "id": "UPS",
              "name": "Armadio UPS",
              "row": "A",
              "u": 12,
              "x": 0.05,
              "y": 3.55,
              "w": 0.8,
              "h": 0.62,
              "devices": [
                {
                  "_uid": "cb3847e0-2d01-4a1a-81e1-14e8958aac83",
                  "id": "ups-01",
                  "name": "ups-01",
                  "type": "alimentazione",
                  "model": "APC Smart-UPS SRT 5000",
                  "ip": "10.0.0.90",
                  "serial": "AS1948",
                  "owner": "Team Infra",
                  "u": 1,
                  "h": 6
                }
              ]
            },
            {
              "_uid": "6099c10e-e901-459e-afc2-3fd81512542a",
              "id": "R04",
              "name": "Rack R04 — Applicativi",
              "row": "B",
              "u": 45,
              "x": 3.6,
              "y": 0.95,
              "w": 0.6,
              "h": 0.6,
              "devices": [
                {
                  "_uid": "f2759bd3-af44-4ecd-adba-d9d0dacd6b3f",
                  "id": "sw-tor-04",
                  "name": "sw-tor-04",
                  "type": "rete",
                  "model": "Cisco C9200-24T",
                  "ip": "10.0.0.14",
                  "serial": "FCW2404",
                  "owner": "Team Rete",
                  "u": 42,
                  "h": 1
                },
                {
                  "_uid": "1027fe57-8e30-4b45-906f-037964fdb507",
                  "id": "srv-app-01",
                  "name": "srv-app-01",
                  "type": "server",
                  "model": "Dell R650",
                  "ip": "10.0.4.61",
                  "serial": "SN-4AP1Q",
                  "owner": "Team Dev",
                  "u": 30,
                  "h": 1
                },
                {
                  "_uid": "59c70429-a461-4586-a6eb-06b7a45cc011",
                  "id": "srv-app-02",
                  "name": "srv-app-02",
                  "type": "server",
                  "model": "Dell R650",
                  "ip": "10.0.4.62",
                  "serial": "SN-4AP2R",
                  "owner": "Team Dev",
                  "u": 28,
                  "h": 1
                }
              ]
            },
            {
              "_uid": "4a67331e-c865-4bdd-8e9b-674767d35cc1",
              "id": "R05",
              "name": "Rack R05 — Storage SAN",
              "row": "B",
              "u": 45,
              "x": 3.6,
              "y": 1.605,
              "w": 0.6,
              "h": 0.6,
              "devices": [
                {
                  "_uid": "9c622c79-f3e9-4038-ad66-d6482c643444",
                  "id": "sw-tor-05",
                  "name": "sw-tor-05",
                  "type": "rete",
                  "model": "Cisco C9200-24T",
                  "ip": "10.0.0.15",
                  "serial": "FCW2405",
                  "owner": "Team Rete",
                  "u": 42,
                  "h": 1
                },
                {
                  "_uid": "cb60b47d-97ef-44a5-8865-edfb2dc6876a",
                  "id": "san-01",
                  "name": "san-01",
                  "type": "storage",
                  "model": "Dell ME5024",
                  "ip": "10.0.5.71",
                  "serial": "ME5-3301",
                  "owner": "Team Infra",
                  "u": 20,
                  "h": 2
                },
                {
                  "_uid": "3fa2e818-dd83-4cc6-9c16-82a6fc7426cf",
                  "id": "san-02",
                  "name": "san-02",
                  "type": "storage",
                  "model": "Dell ME5024",
                  "ip": "10.0.5.72",
                  "serial": "ME5-3302",
                  "owner": "Team Infra",
                  "u": 18,
                  "h": 2
                }
              ]
            },
            {
              "_uid": "7e24fcd1-75e0-4b95-ae21-c6c5283360d2",
              "id": "R06",
              "name": "Rack R06 — Backup",
              "row": "B",
              "u": 45,
              "x": 3.6,
              "y": 2.26,
              "w": 0.6,
              "h": 0.6,
              "devices": [
                {
                  "_uid": "0b7776cd-e726-4f69-a848-6e6e5aa82ca3",
                  "id": "sw-tor-06",
                  "name": "sw-tor-06",
                  "type": "rete",
                  "model": "Cisco C9200-24T",
                  "ip": "10.0.0.16",
                  "serial": "FCW2406",
                  "owner": "Team Rete",
                  "u": 42,
                  "h": 1
                },
                {
                  "_uid": "6ad69c29-d9c6-48be-a62e-17ce4285a6a0",
                  "id": "srv-bck-01",
                  "name": "srv-bck-01",
                  "type": "server",
                  "model": "Dell R750",
                  "ip": "10.0.6.81",
                  "serial": "SN-6BK1S",
                  "owner": "Team Infra",
                  "u": 25,
                  "h": 2
                },
                {
                  "_uid": "6e581a45-2810-409e-94c3-33b899188da8",
                  "id": "lib-01",
                  "name": "lib-01",
                  "type": "storage",
                  "model": "IBM TS4300",
                  "ip": "10.0.6.85",
                  "serial": "TS4-0912",
                  "owner": "Team Infra",
                  "u": 10,
                  "h": 3
                }
              ]
            },
            {
              "_uid": "95ac0cf6-b118-40b3-a658-d7238f34ac27",
              "id": "R07",
              "name": "Rack R07 — Kubernetes",
              "row": "B",
              "u": 45,
              "x": 3.6,
              "y": 2.915,
              "w": 0.6,
              "h": 0.6,
              "devices": [
                {
                  "_uid": "1ebdd2c8-8a18-48cd-817e-7f828f0f2ed5",
                  "id": "sw-tor-07",
                  "name": "sw-tor-07",
                  "type": "rete",
                  "model": "Cisco C9200-24T",
                  "ip": "10.0.0.17",
                  "serial": "FCW2407",
                  "owner": "Team Rete",
                  "u": 42,
                  "h": 1
                },
                {
                  "_uid": "6fb19f75-6597-438a-958c-4820c7723f15",
                  "id": "srv-k8s-01",
                  "name": "srv-k8s-01",
                  "type": "server",
                  "model": "HPE DL360 Gen11",
                  "ip": "10.0.7.91",
                  "serial": "CZJ1301",
                  "owner": "Team Dev",
                  "u": 30,
                  "h": 1
                },
                {
                  "_uid": "548c4145-6545-42fd-9d20-3e0fb4ff92bd",
                  "id": "srv-k8s-02",
                  "name": "srv-k8s-02",
                  "type": "server",
                  "model": "HPE DL360 Gen11",
                  "ip": "10.0.7.92",
                  "serial": "CZJ1302",
                  "owner": "Team Dev",
                  "u": 28,
                  "h": 1
                },
                {
                  "_uid": "96b39ef2-cc98-487f-9f4d-b3f85c30e732",
                  "id": "srv-k8s-03",
                  "name": "srv-k8s-03",
                  "type": "server",
                  "model": "HPE DL360 Gen11",
                  "ip": "10.0.7.93",
                  "serial": "CZJ1303",
                  "owner": "Team Dev",
                  "u": 26,
                  "h": 1
                }
              ]
            },
            {
              "_uid": "7bea8392-6fa2-44b7-9f65-65dae5c5ba5d",
              "id": "R08",
              "name": "Rack R08 — GPU",
              "row": "B",
              "u": 45,
              "x": 3.6,
              "y": 3.57,
              "w": 0.6,
              "h": 0.6,
              "devices": [
                {
                  "_uid": "43fc3ebc-3a5a-4dc5-b8ff-c65fdef6e501",
                  "id": "sw-tor-08",
                  "name": "sw-tor-08",
                  "type": "rete",
                  "model": "Cisco C9200-24T",
                  "ip": "10.0.0.18",
                  "serial": "FCW2408",
                  "owner": "Team Rete",
                  "u": 42,
                  "h": 1
                },
                {
                  "_uid": "b6b78c80-1daa-4877-9768-a635f766a4c2",
                  "id": "srv-gpu-01",
                  "name": "srv-gpu-01",
                  "type": "server",
                  "model": "Supermicro SYS-421GE",
                  "ip": "10.0.8.95",
                  "serial": "SM-GP41",
                  "owner": "Team Dev",
                  "u": 20,
                  "h": 4
                }
              ]
            },
            {
              "_uid": "02a57b83-7c5f-4288-abe2-fb114773eab1",
              "id": "R09",
              "name": "Rack R09 — Test",
              "row": "B",
              "u": 45,
              "x": 3.6,
              "y": 4.225,
              "w": 0.6,
              "h": 0.6,
              "devices": [
                {
                  "_uid": "8262de43-5bf4-4d5b-98eb-308bf9ed763f",
                  "id": "sw-tor-09",
                  "name": "sw-tor-09",
                  "type": "rete",
                  "model": "Cisco C9200-24T",
                  "ip": "10.0.0.19",
                  "serial": "FCW2409",
                  "owner": "Team Rete",
                  "u": 42,
                  "h": 1
                },
                {
                  "_uid": "8c0d2292-d2c2-4938-9b53-156914e0af2a",
                  "id": "srv-test-01",
                  "name": "srv-test-01",
                  "type": "server",
                  "model": "Dell R650",
                  "ip": "10.0.9.97",
                  "serial": "SN-9TS1T",
                  "owner": "Team Dev",
                  "u": 30,
                  "h": 1
                }
              ]
            }
          ]
        },
        {
          "_uid": "f38e181b-77f6-434f-86ab-5cbe35e5b3d4",
          "id": "centro-stella",
          "nome": "Centro Stella",
          "w": 2.79,
          "h": 4.97,
          "area": "13.85 m²",
          "dim": "2.79 × 4.97 m",
          "vani": [
            {
              "x": 0,
              "y": 0,
              "w": 2.79,
              "h": 4.97,
              "porta": {
                "lato": "bottom",
                "x": 1.77,
                "w": 0.87
              }
            }
          ],
          "racks": [
            {
              "_uid": "ae2300fc-4d95-487f-9179-bab477ef0288",
              "id": "CS-R01",
              "name": "Rack CS-R01",
              "row": "A",
              "u": 45,
              "x": 0.45,
              "y": 0.8,
              "w": 0.62,
              "h": 0.55,
              "devices": []
            },
            {
              "_uid": "c5e313fc-3142-445e-80f8-401c0be7e691",
              "id": "CS-R02",
              "name": "Rack CS-R02",
              "row": "A",
              "u": 45,
              "x": 0.45,
              "y": 1.4,
              "w": 0.62,
              "h": 0.55,
              "devices": []
            },
            {
              "_uid": "7c2f777b-b20c-4832-9492-1720635f9000",
              "id": "CS-R03",
              "name": "Rack CS-R03",
              "row": "A",
              "u": 45,
              "x": 0.45,
              "y": 2,
              "w": 0.62,
              "h": 0.55,
              "devices": []
            },
            {
              "_uid": "dcd8fbb4-fc4a-4287-8444-12e81aa8dfa6",
              "id": "CS-R04",
              "name": "Rack CS-R04",
              "row": "A",
              "u": 45,
              "x": 0.45,
              "y": 2.6,
              "w": 0.62,
              "h": 0.55,
              "devices": []
            },
            {
              "_uid": "56dbdba9-c67a-45c0-9d95-2b2c6ae68a6e",
              "id": "CS-R05",
              "name": "Rack CS-R05",
              "row": "A",
              "u": 45,
              "x": 0.45,
              "y": 3.2,
              "w": 0.62,
              "h": 0.55,
              "devices": []
            },
            {
              "_uid": "17e29058-451d-4b2b-91c3-6d6756fe869d",
              "id": "CS-R06",
              "name": "Rack CS-R06",
              "row": "A",
              "u": 45,
              "x": 1.1,
              "y": 0.8,
              "w": 0.62,
              "h": 0.55,
              "devices": []
            },
            {
              "_uid": "630cce3a-dfd6-4889-bb6c-2b77dfe7d0ab",
              "id": "CS-Q01",
              "name": "Quadro elettrico",
              "row": "—",
              "u": 6,
              "x": 2.15,
              "y": 2.7,
              "w": 0.42,
              "h": 0.42,
              "devices": []
            }
          ]
        },
        {
          "_uid": "dffe39f3-5e30-4388-9295-4352b191a311",
          "id": "frontend",
          "nome": "Frontend",
          "w": 4.89,
          "h": 7.5,
          "area": "36.63 m²",
          "dim": "4.89 × 7.50 m",
          "vani": [
            {
              "x": 0,
              "y": 0,
              "w": 4.89,
              "h": 7.5,
              "porta": {
                "lato": "bottom",
                "x": 3.85,
                "w": 0.9
              }
            }
          ],
          "racks": [
            {
              "_uid": "d0ba2dd9-456b-4e29-86ed-f869fd094ab3",
              "id": "FE-R01",
              "name": "Rack FE-R01",
              "row": "A",
              "u": 45,
              "x": 1,
              "y": 0.3,
              "w": 0.68,
              "h": 0.66,
              "devices": []
            },
            {
              "_uid": "dc7fae91-68e0-4dd5-bd6d-ce401b97d471",
              "id": "FE-R02",
              "name": "Rack FE-R02",
              "row": "A",
              "u": 45,
              "x": 1,
              "y": 1,
              "w": 0.68,
              "h": 0.66,
              "devices": []
            },
            {
              "_uid": "de672ad7-1cb5-4b04-aa5f-ce670802285b",
              "id": "FE-R03",
              "name": "Rack FE-R03",
              "row": "A",
              "u": 45,
              "x": 1,
              "y": 1.7,
              "w": 0.68,
              "h": 0.66,
              "devices": []
            },
            {
              "_uid": "6a7dfb5c-6d3c-4ac2-a059-8f028ebc5a10",
              "id": "FE-R04",
              "name": "Rack FE-R04",
              "row": "A",
              "u": 45,
              "x": 1,
              "y": 2.3999999999999995,
              "w": 0.68,
              "h": 0.66,
              "devices": []
            },
            {
              "_uid": "b0c4cb4a-f5b4-4749-994b-7b69ad707119",
              "id": "FE-R05",
              "name": "Rack FE-R05",
              "row": "A",
              "u": 45,
              "x": 1,
              "y": 3.0999999999999996,
              "w": 0.68,
              "h": 0.66,
              "devices": []
            },
            {
              "_uid": "678b450d-6d7c-49fb-9262-fbc9f0b779ed",
              "id": "FE-R06",
              "name": "Rack FE-R06",
              "row": "A",
              "u": 45,
              "x": 1,
              "y": 3.8,
              "w": 0.68,
              "h": 0.66,
              "devices": []
            },
            {
              "_uid": "4185346e-6579-44b2-8729-4d15891a4374",
              "id": "FE-R07",
              "name": "Rack FE-R07",
              "row": "A",
              "u": 45,
              "x": 1,
              "y": 4.499999999999999,
              "w": 0.68,
              "h": 0.66,
              "devices": []
            },
            {
              "_uid": "4f331a1d-b0ac-495d-8c17-5fd2997ce1e2",
              "id": "FE-R08",
              "name": "Rack FE-R08",
              "row": "A",
              "u": 45,
              "x": 1,
              "y": 5.199999999999999,
              "w": 0.68,
              "h": 0.66,
              "devices": []
            },
            {
              "_uid": "b5e98763-9f25-4cd8-8dcf-b6dfb99e1d4c",
              "id": "FE-R09",
              "name": "Rack FE-R09",
              "row": "A",
              "u": 45,
              "x": 1,
              "y": 5.8999999999999995,
              "w": 0.68,
              "h": 0.66,
              "devices": []
            },
            {
              "_uid": "e511d60a-791f-426b-b9ec-5513bc171b85",
              "id": "FE-R10",
              "name": "Rack FE-R10",
              "row": "A",
              "u": 45,
              "x": 1,
              "y": 6.6,
              "w": 0.68,
              "h": 0.66,
              "devices": []
            },
            {
              "_uid": "5ee1b7ee-1965-4798-b0d8-dd40fd45328e",
              "id": "FE-R11",
              "name": "Rack FE-R11",
              "row": "B",
              "u": 45,
              "x": 3.85,
              "y": 0.8,
              "w": 0.62,
              "h": 0.53,
              "devices": []
            },
            {
              "_uid": "b2929e83-f9a3-465f-a650-d4cb3c345888",
              "id": "FE-R12",
              "name": "Rack FE-R12",
              "row": "B",
              "u": 45,
              "x": 3.85,
              "y": 1.35,
              "w": 0.62,
              "h": 0.53,
              "devices": []
            },
            {
              "_uid": "068df71b-ee45-40aa-a2af-da649b9e76c2",
              "id": "FE-R13",
              "name": "Rack FE-R13",
              "row": "B",
              "u": 45,
              "x": 3.85,
              "y": 1.9000000000000001,
              "w": 0.62,
              "h": 0.53,
              "devices": []
            },
            {
              "_uid": "de865797-2c36-44de-8d2c-93e420808617",
              "id": "FE-R14",
              "name": "Rack FE-R14",
              "row": "B",
              "u": 45,
              "x": 3.85,
              "y": 2.45,
              "w": 0.62,
              "h": 0.53,
              "devices": []
            },
            {
              "_uid": "01ed30bc-56f7-4b60-bb7c-eeb326cd1854",
              "id": "FE-R15",
              "name": "Rack FE-R15",
              "row": "B",
              "u": 45,
              "x": 3.85,
              "y": 3,
              "w": 0.62,
              "h": 0.53,
              "devices": []
            },
            {
              "_uid": "dac72949-dde7-47c5-bb69-a28264517c44",
              "id": "FE-R16",
              "name": "Rack FE-R16",
              "row": "B",
              "u": 45,
              "x": 3.85,
              "y": 3.55,
              "w": 0.62,
              "h": 0.53,
              "devices": []
            },
            {
              "_uid": "b06f034e-b94b-4a8d-adce-5ab89f5fc7d3",
              "id": "FE-R17",
              "name": "Rack FE-R17",
              "row": "B",
              "u": 45,
              "x": 3.85,
              "y": 4.1000000000000005,
              "w": 0.62,
              "h": 0.53,
              "devices": []
            },
            {
              "_uid": "06ba0555-b886-46b6-8390-3de88165a051",
              "id": "FE-R18",
              "name": "Rack FE-R18",
              "row": "B",
              "u": 45,
              "x": 3.85,
              "y": 4.65,
              "w": 0.62,
              "h": 0.53,
              "devices": []
            },
            {
              "_uid": "617fa780-7c12-4032-af06-320c330db77a",
              "id": "FE-R19",
              "name": "Rack FE-R19",
              "row": "B",
              "u": 45,
              "x": 3.85,
              "y": 5.2,
              "w": 0.62,
              "h": 0.53,
              "devices": []
            },
            {
              "_uid": "465baa08-a0c9-43f5-8448-a3accefdf988",
              "id": "FE-Q01",
              "name": "Armadio tecnico",
              "row": "—",
              "u": 12,
              "x": 3,
              "y": 0.05,
              "w": 0.8,
              "h": 0.55,
              "devices": []
            }
          ]
        },
        {
          "_uid": "9752009f-cf79-4d25-8e28-1295cdd8f7f3",
          "id": "saletta-nuova",
          "nome": "Saletta Nuova",
          "w": 4.23,
          "h": 14.2,
          "area": "57.19 m² (2 vani)",
          "dim": "4.23 × 3.65 m + 4.23 × 10.43 m",
          "vani": [
            {
              "x": 0,
              "y": 0,
              "w": 4.23,
              "h": 3.65,
              "porta": {
                "lato": "bottom",
                "x": 2.2,
                "w": 0.9
              }
            },
            {
              "x": 0,
              "y": 3.77,
              "w": 4.23,
              "h": 10.43,
              "porta": {
                "lato": "right",
                "y": 6.55,
                "w": 0.96
              }
            }
          ],
          "racks": [
            {
              "_uid": "1d361c6f-22ad-45dd-bf6f-070c30b3a8d1",
              "id": "SN-R01",
              "name": "Rack SN-R01",
              "row": "A",
              "u": 45,
              "x": 3.55,
              "y": 0.35,
              "w": 0.6,
              "h": 0.62,
              "devices": []
            },
            {
              "_uid": "c01b89d5-27b4-4ccd-bfda-46257bef167c",
              "id": "SN-R02",
              "name": "Rack SN-R02",
              "row": "A",
              "u": 45,
              "x": 3.55,
              "y": 1.15,
              "w": 0.6,
              "h": 0.62,
              "devices": []
            },
            {
              "_uid": "0082b9fe-9f7b-452a-b473-5f57db4a930b",
              "id": "SN-R03",
              "name": "Rack SN-R03",
              "row": "A",
              "u": 45,
              "x": 3.55,
              "y": 1.95,
              "w": 0.6,
              "h": 0.62,
              "devices": []
            },
            {
              "_uid": "69b01ec3-bd99-427d-a297-1eb88ca19713",
              "id": "SN-R04",
              "name": "Rack SN-R04",
              "row": "A",
              "u": 45,
              "x": 3.55,
              "y": 2.75,
              "w": 0.6,
              "h": 0.62,
              "devices": []
            },
            {
              "_uid": "46229a89-4089-4813-a9bb-86353b483ba2",
              "id": "SN-R05",
              "name": "Rack SN-R05",
              "row": "A",
              "u": 45,
              "x": 0.45,
              "y": 1.3,
              "w": 0.6,
              "h": 0.62,
              "devices": []
            },
            {
              "_uid": "6f44bf0a-cccc-4d9f-8916-027c102e977f",
              "id": "SN-R06",
              "name": "Rack SN-R06",
              "row": "A",
              "u": 45,
              "x": 0.45,
              "y": 2.5,
              "w": 0.6,
              "h": 0.62,
              "devices": []
            },
            {
              "_uid": "1b08a02e-98bf-4e96-be1f-f87c49709bf7",
              "id": "SN-R07",
              "name": "Rack SN-R07",
              "row": "B",
              "u": 45,
              "x": 0.65,
              "y": 4.4,
              "w": 0.68,
              "h": 0.63,
              "devices": []
            },
            {
              "_uid": "44113978-248c-4903-9a02-753ad3672929",
              "id": "SN-R08",
              "name": "Rack SN-R08",
              "row": "B",
              "u": 45,
              "x": 0.65,
              "y": 5.0600000000000005,
              "w": 0.68,
              "h": 0.63,
              "devices": []
            },
            {
              "_uid": "b63edf88-4a21-4d50-a6c9-669ca6ebc6eb",
              "id": "SN-R09",
              "name": "Rack SN-R09",
              "row": "B",
              "u": 45,
              "x": 0.65,
              "y": 5.720000000000001,
              "w": 0.68,
              "h": 0.63,
              "devices": []
            },
            {
              "_uid": "b101766b-b997-480c-a561-ff686ec99213",
              "id": "SN-R10",
              "name": "Rack SN-R10",
              "row": "B",
              "u": 45,
              "x": 0.65,
              "y": 6.380000000000001,
              "w": 0.68,
              "h": 0.63,
              "devices": []
            },
            {
              "_uid": "f4683b7e-b590-4469-8842-f0809e78f315",
              "id": "SN-R11",
              "name": "Rack SN-R11",
              "row": "B",
              "u": 45,
              "x": 0.65,
              "y": 7.040000000000001,
              "w": 0.68,
              "h": 0.63,
              "devices": []
            },
            {
              "_uid": "bbb9d525-e082-45e2-9ed1-81f0d059a2f6",
              "id": "SN-R12",
              "name": "Rack SN-R12",
              "row": "B",
              "u": 45,
              "x": 0.65,
              "y": 7.700000000000001,
              "w": 0.68,
              "h": 0.63,
              "devices": []
            },
            {
              "_uid": "a9fd761c-43ec-4c55-9ee2-60ae97ccfd15",
              "id": "SN-R13",
              "name": "Rack SN-R13",
              "row": "B",
              "u": 45,
              "x": 0.65,
              "y": 8.36,
              "w": 0.68,
              "h": 0.63,
              "devices": []
            },
            {
              "_uid": "0993ff79-4734-4368-8e49-4f2c5aee4954",
              "id": "SN-R14",
              "name": "Rack SN-R14",
              "row": "B",
              "u": 45,
              "x": 0.65,
              "y": 9.02,
              "w": 0.68,
              "h": 0.63,
              "devices": []
            },
            {
              "_uid": "01d09ae3-da5a-44c4-a3f0-704657c960f0",
              "id": "SN-R15",
              "name": "Rack SN-R15",
              "row": "B",
              "u": 45,
              "x": 0.65,
              "y": 9.68,
              "w": 0.68,
              "h": 0.63,
              "devices": []
            },
            {
              "_uid": "0e059487-7fbc-400d-aaad-2bf0568becd5",
              "id": "SN-R16",
              "name": "Rack SN-R16",
              "row": "B",
              "u": 45,
              "x": 0.65,
              "y": 10.34,
              "w": 0.68,
              "h": 0.63,
              "devices": []
            },
            {
              "_uid": "758e65eb-68f2-43cc-a909-cd4dda85abea",
              "id": "SN-R17",
              "name": "Rack SN-R17",
              "row": "C",
              "u": 45,
              "x": 3.05,
              "y": 4.4,
              "w": 0.62,
              "h": 0.6,
              "devices": []
            },
            {
              "_uid": "52c9278a-b1b0-4f0e-abbe-6160c7cb9537",
              "id": "SN-R18",
              "name": "Rack SN-R18",
              "row": "C",
              "u": 45,
              "x": 3.05,
              "y": 5.03,
              "w": 0.62,
              "h": 0.6,
              "devices": []
            },
            {
              "_uid": "50f2ae96-e427-409d-a06a-f467c859f185",
              "id": "SN-R19",
              "name": "Rack SN-R19",
              "row": "C",
              "u": 45,
              "x": 3.05,
              "y": 5.66,
              "w": 0.62,
              "h": 0.6,
              "devices": []
            },
            {
              "_uid": "81bf7803-035a-496c-a5c4-8b038981b892",
              "id": "SN-R20",
              "name": "Rack SN-R20",
              "row": "C",
              "u": 45,
              "x": 3.05,
              "y": 6.290000000000001,
              "w": 0.62,
              "h": 0.6,
              "devices": []
            },
            {
              "_uid": "4a8d7a03-12b3-442f-ab0f-312b984904a0",
              "id": "SN-R21",
              "name": "Rack SN-R21",
              "row": "C",
              "u": 45,
              "x": 3.05,
              "y": 6.92,
              "w": 0.62,
              "h": 0.6,
              "devices": []
            },
            {
              "_uid": "106d7a9c-d11e-4372-a87d-f8a5d816dd37",
              "id": "SN-R22",
              "name": "Rack SN-R22",
              "row": "C",
              "u": 45,
              "x": 3.05,
              "y": 7.550000000000001,
              "w": 0.62,
              "h": 0.6,
              "devices": []
            },
            {
              "_uid": "f392c8ff-a277-46d6-825b-d923dd6d117d",
              "id": "SN-R23",
              "name": "Rack SN-R23",
              "row": "B",
              "u": 45,
              "x": 0.7,
              "y": 11.85,
              "w": 0.62,
              "h": 0.62,
              "devices": []
            },
            {
              "_uid": "f9df157c-5d4c-4c70-a0bf-8ea89ea8fa7a",
              "id": "SN-R24",
              "name": "Rack SN-R24",
              "row": "C",
              "u": 45,
              "x": 3,
              "y": 11.85,
              "w": 0.6,
              "h": 0.6,
              "devices": []
            }
          ]
        }
      ]
    },
    {
      "_uid": "2e6ef8ca-bc00-45c7-8f45-105df2fca4a3",
      "id": "pomezia-h0",
      "nome": "Pomezia — H0",
      "sale": [
        {
          "_uid": "ba5977a1-db44-4927-a9ba-bce366130605",
          "id": "h0",
          "nome": "H0",
          "w": 7.58,
          "h": 8.18,
          "area": "61.68 m²",
          "dim": "7.58 × 8.18 m",
          "vani": [
            {
              "x": 0,
              "y": 0,
              "w": 7.58,
              "h": 8.18,
              "porta": {
                "lato": "left",
                "y": 0.71,
                "w": 0.8
              },
              "porta2": {
                "lato": "bottom",
                "x": 4.3,
                "w": 3
              }
            }
          ],
          "racks": [
            {
              "_uid": "c168a897-960e-4b0f-84bc-cb35c3eda6d5",
              "id": "H0-R01",
              "name": "Rack H0-R01",
              "row": "A",
              "u": 45,
              "x": 1.95,
              "y": 2.35,
              "w": 0.5,
              "h": 0.64,
              "devices": []
            },
            {
              "_uid": "9218e12a-7ad8-49f5-ad79-fd16908c6760",
              "id": "H0-R02",
              "name": "Rack H0-R02",
              "row": "A",
              "u": 45,
              "x": 1.95,
              "y": 3.0100000000000002,
              "w": 0.5,
              "h": 0.64,
              "devices": []
            },
            {
              "_uid": "90fe4b68-d9e5-41b2-9b4b-b81be0965c4b",
              "id": "H0-R03",
              "name": "Rack H0-R03",
              "row": "A",
              "u": 45,
              "x": 1.95,
              "y": 3.67,
              "w": 0.5,
              "h": 0.64,
              "devices": []
            },
            {
              "_uid": "eda0cd3d-47d8-47c1-8d51-ce1bf063b2ef",
              "id": "H0-R04",
              "name": "Rack H0-R04",
              "row": "A",
              "u": 45,
              "x": 1.95,
              "y": 4.33,
              "w": 0.5,
              "h": 0.64,
              "devices": []
            },
            {
              "_uid": "ed23db2e-90e9-4dd7-aada-58c1b95c6205",
              "id": "H0-R05",
              "name": "Rack H0-R05",
              "row": "A",
              "u": 45,
              "x": 1.95,
              "y": 4.99,
              "w": 0.5,
              "h": 0.64,
              "devices": []
            },
            {
              "_uid": "de1515cd-c684-428d-8357-73943b317ffb",
              "id": "H0-R06",
              "name": "Rack H0-R06",
              "row": "A",
              "u": 45,
              "x": 1.95,
              "y": 5.65,
              "w": 0.5,
              "h": 0.64,
              "devices": []
            },
            {
              "_uid": "041b74ad-162b-4c4d-8481-61931010643e",
              "id": "H0-R07",
              "name": "Rack H0-R07",
              "row": "A",
              "u": 45,
              "x": 1.95,
              "y": 6.3100000000000005,
              "w": 0.5,
              "h": 0.64,
              "devices": []
            },
            {
              "_uid": "a61d75a8-9502-4837-8734-9b3b2c4fda15",
              "id": "H0-R08",
              "name": "Rack H0-R08",
              "row": "A",
              "u": 45,
              "x": 1.95,
              "y": 6.970000000000001,
              "w": 0.5,
              "h": 0.64,
              "devices": []
            },
            {
              "_uid": "98765e6e-aa2d-4064-a0f3-e3dbb2b7c244",
              "id": "H0-R09",
              "name": "Rack H0-R09",
              "row": "B",
              "u": 45,
              "x": 3.75,
              "y": 2.35,
              "w": 0.5,
              "h": 0.64,
              "devices": []
            },
            {
              "_uid": "f386f30a-b7a2-4b15-83d1-786e8442a06b",
              "id": "H0-R10",
              "name": "Rack H0-R10",
              "row": "B",
              "u": 45,
              "x": 3.75,
              "y": 3.0100000000000002,
              "w": 0.5,
              "h": 0.64,
              "devices": []
            },
            {
              "_uid": "ecf1a7b7-f05b-428f-9dac-ac1b14db82b2",
              "id": "H0-R11",
              "name": "Rack H0-R11",
              "row": "B",
              "u": 45,
              "x": 3.75,
              "y": 3.67,
              "w": 0.5,
              "h": 0.64,
              "devices": []
            },
            {
              "_uid": "6cda8bfa-bcc7-4066-bbc1-d8503ab83879",
              "id": "H0-R12",
              "name": "Rack H0-R12",
              "row": "B",
              "u": 45,
              "x": 3.75,
              "y": 4.33,
              "w": 0.5,
              "h": 0.64,
              "devices": []
            },
            {
              "_uid": "98439447-9ef5-4e21-9610-e6e566f20297",
              "id": "H0-R13",
              "name": "Rack H0-R13",
              "row": "B",
              "u": 45,
              "x": 3.75,
              "y": 4.99,
              "w": 0.5,
              "h": 0.64,
              "devices": []
            },
            {
              "_uid": "aff1a052-4419-4c8e-8953-544052a8167a",
              "id": "H0-R14",
              "name": "Rack H0-R14",
              "row": "B",
              "u": 45,
              "x": 3.75,
              "y": 5.65,
              "w": 0.5,
              "h": 0.64,
              "devices": []
            },
            {
              "_uid": "7c2549ee-8c30-4028-b4a7-4119d70fcd1e",
              "id": "H0-R15",
              "name": "Rack H0-R15",
              "row": "B",
              "u": 45,
              "x": 3.75,
              "y": 6.3100000000000005,
              "w": 0.5,
              "h": 0.64,
              "devices": []
            },
            {
              "_uid": "24c0808d-ba9e-495b-9ce3-406b1ab2703f",
              "id": "H0-R16",
              "name": "Rack H0-R16",
              "row": "B",
              "u": 45,
              "x": 3.75,
              "y": 6.970000000000001,
              "w": 0.5,
              "h": 0.64,
              "devices": []
            },
            {
              "_uid": "3d1f39b8-2342-4074-b624-99286f0280b4",
              "id": "H0-R17",
              "name": "Rack H0-R17",
              "row": "C",
              "u": 45,
              "x": 5,
              "y": 2.35,
              "w": 0.5,
              "h": 0.64,
              "devices": []
            },
            {
              "_uid": "09bc0b33-8d24-48c6-a3b1-d44eac548855",
              "id": "H0-R18",
              "name": "Rack H0-R18",
              "row": "C",
              "u": 45,
              "x": 5,
              "y": 3.0100000000000002,
              "w": 0.5,
              "h": 0.64,
              "devices": []
            },
            {
              "_uid": "e549f22d-34dd-494d-bae0-f3fe6729cb15",
              "id": "H0-R19",
              "name": "Rack H0-R19",
              "row": "C",
              "u": 45,
              "x": 5,
              "y": 3.67,
              "w": 0.5,
              "h": 0.64,
              "devices": []
            },
            {
              "_uid": "0d5171f1-0e4f-40ba-b7dc-daeafd0c65e9",
              "id": "H0-R20",
              "name": "Rack H0-R20",
              "row": "C",
              "u": 45,
              "x": 5,
              "y": 4.33,
              "w": 0.5,
              "h": 0.64,
              "devices": []
            },
            {
              "_uid": "052a7312-6733-47f3-b963-4c880045d376",
              "id": "H0-R21",
              "name": "Rack H0-R21",
              "row": "C",
              "u": 45,
              "x": 5,
              "y": 4.99,
              "w": 0.5,
              "h": 0.64,
              "devices": []
            },
            {
              "_uid": "90bcb041-caf9-4f95-9b3b-cabbaa41e883",
              "id": "H0-R22",
              "name": "Rack H0-R22",
              "row": "C",
              "u": 45,
              "x": 5,
              "y": 5.65,
              "w": 0.5,
              "h": 0.64,
              "devices": []
            },
            {
              "_uid": "40e7b14e-8207-45eb-b1e6-bddb19eb78da",
              "id": "H0-R23",
              "name": "Rack H0-R23",
              "row": "C",
              "u": 45,
              "x": 5,
              "y": 6.3100000000000005,
              "w": 0.5,
              "h": 0.64,
              "devices": []
            },
            {
              "_uid": "698b3bb3-7aac-4f8a-ad6a-95c841e450de",
              "id": "H0-R24",
              "name": "Rack H0-R24",
              "row": "C",
              "u": 45,
              "x": 5,
              "y": 6.970000000000001,
              "w": 0.5,
              "h": 0.64,
              "devices": []
            },
            {
              "_uid": "d5fe7f26-123f-450b-ad77-3589e9c2b3e3",
              "id": "H0-Q01",
              "name": "Armadio a muro",
              "row": "—",
              "u": 12,
              "x": 6.95,
              "y": 2.3,
              "w": 0.45,
              "h": 1.9,
              "devices": []
            }
          ]
        }
      ]
    },
    {
      "_uid": "a9e8b864-01e0-41b3-85ea-a52b1fadc5b2",
      "id": "oriolo-romano",
      "nome": "Oriolo Romano",
      "sale": [
        {
          "_uid": "84222257-5bb3-405d-bdf8-de4bf2f12e28",
          "id": "sala-oriolo",
          "nome": "Sala Oriolo",
          "w": 9.2,
          "h": 6,
          "area": "55.20 m²",
          "dim": "9.20 × 6.00 m",
          "segnaposto": false,
          "vani": [
            {
              "x": 0,
              "y": 0,
              "w": 9.2,
              "h": 6,
              "porta": {
                "lato": "left",
                "y": 4.9,
                "w": 0.9
              }
            }
          ],
          "racks": [
            {
              "_uid": "be71237a-7c62-4e37-b041-b93b5b196551",
              "id": "RO-A01",
              "name": "Rack RO-A01",
              "row": "A",
              "u": 45,
              "x": 0.3,
              "y": 0.15,
              "w": 0.63,
              "h": 0.72,
              "seriali": [
                "2020047937"
              ],
              "devices": [
                {
                  "_uid": "dcbd34d0-f455-47cc-9dca-ce2d1d36a677",
                  "id": "DR-ESX1",
                  "name": "DR-ESX1",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 44,
                  "h": 1
                },
                {
                  "_uid": "9a804b6b-b393-4482-9ce2-f4f74f2e9473",
                  "id": "DR-ESX2",
                  "name": "DR-ESX2",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 42,
                  "h": 1
                }
              ]
            },
            {
              "_uid": "6b9f9d3e-a9d0-4160-bab1-26d6ffd13223",
              "id": "RO-A02",
              "name": "Rack RO-A02",
              "row": "A",
              "u": 45,
              "x": 0.9299999999999999,
              "y": 0.15,
              "w": 0.63,
              "h": 0.72,
              "seriali": [
                "2020053938"
              ],
              "devices": [
                {
                  "_uid": "2ddfae8d-a716-45a5-810e-1d30c643a31a",
                  "id": "DELL EMC UNITY XT",
                  "name": "DELL EMC UNITY XT",
                  "type": "storage",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 41,
                  "h": 4
                }
              ]
            },
            {
              "_uid": "a33fbb60-ad62-44ac-b9ad-d2b896c078c4",
              "id": "RO-A03",
              "name": "Rack RO-A03",
              "row": "A",
              "u": 45,
              "x": 1.56,
              "y": 0.15,
              "w": 0.63,
              "h": 0.72,
              "seriali": [
                "2020044486"
              ],
              "devices": [
                {
                  "_uid": "9d2132e0-96b8-48c5-90d8-e329fb7bf758",
                  "id": "ISILON",
                  "name": "ISILON",
                  "type": "storage",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 41,
                  "h": 4
                }
              ]
            },
            {
              "_uid": "2bee2b4f-7944-44aa-9425-6d6e3c80b6dc",
              "id": "RO-A04",
              "name": "Rack RO-A04",
              "row": "A",
              "u": 45,
              "x": 2.19,
              "y": 0.15,
              "w": 0.63,
              "h": 0.72,
              "seriali": [
                "2020044485"
              ],
              "devices": [
                {
                  "_uid": "4ac5b01f-4fb6-4e17-95c3-b1346c6ded1a",
                  "id": "VNX",
                  "name": "VNX",
                  "type": "storage",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 41,
                  "h": 4
                }
              ]
            },
            {
              "_uid": "417ee265-cc29-48a7-9544-293c58afb260",
              "id": "RO-A05",
              "name": "Rack RO-A05",
              "row": "A",
              "u": 45,
              "x": 2.82,
              "y": 0.15,
              "w": 0.63,
              "h": 0.72,
              "seriali": [
                "2006029486"
              ],
              "devices": [
                {
                  "_uid": "aa5f6eab-bf16-4633-a5ea-b2d4d68d5cbf",
                  "id": "DR-ESX04",
                  "name": "DR-ESX04",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 44,
                  "h": 1
                },
                {
                  "_uid": "1ffb9806-a9a3-4811-b3e6-b5dfc900c660",
                  "id": "CISCO UCS",
                  "name": "CISCO UCS",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 42,
                  "h": 1
                },
                {
                  "_uid": "f4496fa0-c756-460b-8a82-fa905c3360cd",
                  "id": "DR-ESX05",
                  "name": "DR-ESX05",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 40,
                  "h": 1
                },
                {
                  "_uid": "2ca50de5-fbeb-42b5-8a25-d0e2cfd94d3e",
                  "id": "CISCO 4402",
                  "name": "CISCO 4402",
                  "type": "rete",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 38,
                  "h": 1
                }
              ]
            },
            {
              "_uid": "ac94bde5-5da7-48e5-bfd8-ba30a25975af",
              "id": "RO4066",
              "name": "Rack RO4066",
              "row": "A",
              "u": 45,
              "x": 3.4499999999999997,
              "y": 0.15,
              "w": 0.63,
              "h": 0.72,
              "seriali": [
                "2006004084"
              ],
              "devices": [
                {
                  "_uid": "31667164-15f1-4bdc-bb5b-5aa7b07e4b82",
                  "id": "ALTEON 5208",
                  "name": "ALTEON 5208",
                  "type": "rete",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 44,
                  "h": 1
                },
                {
                  "_uid": "7c908904-894e-4b31-a7d0-3ceeade8a0c3",
                  "id": "FORTIGATE 800C 1",
                  "name": "FORTIGATE 800C 1",
                  "type": "firewall",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 42,
                  "h": 1
                },
                {
                  "_uid": "5a2c7877-eb54-482c-a5de-18fedf49fe09",
                  "id": "SWITCH CMCOLL 1",
                  "name": "SWITCH CMCOLL 1",
                  "type": "rete",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 40,
                  "h": 1
                },
                {
                  "_uid": "055e0a2d-8e1e-43dd-ac8a-1c8eb56fe0b7",
                  "id": "FORTIGATE 600E 1",
                  "name": "FORTIGATE 600E 1",
                  "type": "firewall",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 38,
                  "h": 1
                },
                {
                  "_uid": "f2c98986-eeb4-464c-99cd-061929581410",
                  "id": "SPIDDRDB1",
                  "name": "SPIDDRDB1",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 36,
                  "h": 1
                },
                {
                  "_uid": "4880fe3b-0c6a-47b5-b03e-b557c4a0f741",
                  "id": "SPIDDRFE1",
                  "name": "SPIDDRFE1",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 34,
                  "h": 1
                },
                {
                  "_uid": "247607a1-1f09-4d60-bc11-bdcdd05275dd",
                  "id": "SPIDDRBE1",
                  "name": "SPIDDRBE1",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 32,
                  "h": 1
                },
                {
                  "_uid": "3cabb904-aa5b-4d78-8cb9-dc9d069ca4ba",
                  "id": "SPIDDRDIR1",
                  "name": "SPIDDRDIR1",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 30,
                  "h": 1
                }
              ]
            },
            {
              "_uid": "ccde61e2-660d-4788-b7df-7821c784b2ad",
              "id": "RO4037",
              "name": "Rack RO4037",
              "row": "A",
              "u": 45,
              "x": 4.08,
              "y": 0.15,
              "w": 0.63,
              "h": 0.72,
              "seriali": [
                "2006004088"
              ],
              "devices": [
                {
                  "_uid": "7452cb2d-21ff-484d-ad45-ad96dc6dace5",
                  "id": "FORTIGATE 800C 2",
                  "name": "FORTIGATE 800C 2",
                  "type": "firewall",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 44,
                  "h": 1
                },
                {
                  "_uid": "d898dfb8-4824-4137-a274-776fed7f0252",
                  "id": "SWITCH CMCOLL 2",
                  "name": "SWITCH CMCOLL 2",
                  "type": "rete",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 42,
                  "h": 1
                },
                {
                  "_uid": "de490e61-9a00-4cca-b86e-10a87888f279",
                  "id": "ALTEON 5208 2",
                  "name": "ALTEON 5208 2",
                  "type": "rete",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 40,
                  "h": 1
                },
                {
                  "_uid": "9b4ba69e-17de-4d08-93ae-2432edc1e584",
                  "id": "FORTIGATE 600E 2",
                  "name": "FORTIGATE 600E 2",
                  "type": "firewall",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 38,
                  "h": 1
                },
                {
                  "_uid": "2c751695-3cc6-43c7-9e78-df99273e355c",
                  "id": "CISCO ISR4400",
                  "name": "CISCO ISR4400",
                  "type": "rete",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 36,
                  "h": 1
                },
                {
                  "_uid": "64d96bf9-306c-4a96-a6c2-e38cac040f1b",
                  "id": "MEINBERG",
                  "name": "MEINBERG",
                  "type": "altro",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 34,
                  "h": 1
                },
                {
                  "_uid": "21448f36-1ee1-414b-9c09-afbdeb281cdc",
                  "id": "SPIDDRDB2",
                  "name": "SPIDDRDB2",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 32,
                  "h": 1
                },
                {
                  "_uid": "2bf88b8a-ed53-4d0f-8d3a-4811efba8a9b",
                  "id": "SPIDDRFE2",
                  "name": "SPIDDRFE2",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 30,
                  "h": 1
                },
                {
                  "_uid": "57c57331-7680-4475-895c-10947fdc4c22",
                  "id": "SPIDDRBE2",
                  "name": "SPIDDRBE2",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 28,
                  "h": 1
                },
                {
                  "_uid": "1c21320b-70a5-4633-8647-24e0f9fd41af",
                  "id": "SPIDDRDIR2",
                  "name": "SPIDDRDIR2",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 26,
                  "h": 1
                }
              ]
            },
            {
              "_uid": "abc9c98c-dfee-410b-bb45-1522de86956f",
              "id": "RO4067",
              "name": "Rack RO4067",
              "row": "A",
              "u": 45,
              "x": 4.71,
              "y": 0.15,
              "w": 0.63,
              "h": 0.72,
              "seriali": [
                "2006004090"
              ],
              "devices": [
                {
                  "_uid": "2ae0f930-8ef1-4efc-907f-e2d7424c9a79",
                  "id": "DR-PECESXI5",
                  "name": "DR-PECESXI5",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 44,
                  "h": 1
                },
                {
                  "_uid": "2d2b5b84-fb23-4d4e-89d6-2949c9ac4ed3",
                  "id": "DR-PECESXI3",
                  "name": "DR-PECESXI3",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 42,
                  "h": 1
                },
                {
                  "_uid": "af44f4ba-20c9-4537-baad-cb590325ca51",
                  "id": "DR-PECHSM1",
                  "name": "DR-PECHSM1",
                  "type": "altro",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 40,
                  "h": 1
                },
                {
                  "_uid": "d39e5d0c-cb8c-4d84-9969-51a61f943705",
                  "id": "DR-PECPOP1",
                  "name": "DR-PECPOP1",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 38,
                  "h": 1
                }
              ]
            },
            {
              "_uid": "de294b33-cef3-42c1-9893-b9406c5a8ad3",
              "id": "RO4082",
              "name": "Rack RO4082",
              "row": "A",
              "u": 45,
              "x": 5.34,
              "y": 0.15,
              "w": 0.63,
              "h": 0.72,
              "seriali": [
                "2006004091"
              ],
              "devices": [
                {
                  "_uid": "fb53faaf-4c24-429c-90d6-df79b4f9d3e6",
                  "id": "CRYPTO-HSM-TEST",
                  "name": "CRYPTO-HSM-TEST",
                  "type": "altro",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 44,
                  "h": 1
                },
                {
                  "_uid": "07a8b7ab-bd5b-4649-bd6e-a1302154cc1d",
                  "id": "CAOP-DRFE01",
                  "name": "CAOP-DRFE01",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 42,
                  "h": 1
                },
                {
                  "_uid": "00a7df7f-b4cf-4e1d-bf3e-3e3f1777f26d",
                  "id": "CRYPTO-HSM1",
                  "name": "CRYPTO-HSM1",
                  "type": "altro",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 40,
                  "h": 1
                },
                {
                  "_uid": "ba4af358-16f8-4fd8-8625-671eb05f0c48",
                  "id": "LUNA HSM 1",
                  "name": "LUNA HSM 1",
                  "type": "altro",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 38,
                  "h": 1
                },
                {
                  "_uid": "ee5c38e4-e993-45f7-91cb-b0ff97d72923",
                  "id": "LUNA HSM 2",
                  "name": "LUNA HSM 2",
                  "type": "altro",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 36,
                  "h": 1
                },
                {
                  "_uid": "c0cc1ebe-782c-477f-b8a6-f6ac3e022a6e",
                  "id": "DR-PECESXI2",
                  "name": "DR-PECESXI2",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 34,
                  "h": 1
                },
                {
                  "_uid": "12eff825-fe72-414a-add7-c9b111582d49",
                  "id": "DR-PECPOP2",
                  "name": "DR-PECPOP2",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 32,
                  "h": 1
                },
                {
                  "_uid": "973baece-8307-4868-ae45-e992e7d9e3c7",
                  "id": "CONSERVAZIONE LINEA1 A",
                  "name": "CONSERVAZIONE LINEA1 A",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 30,
                  "h": 1
                },
                {
                  "_uid": "c637d9c3-1453-4934-a740-fe15c66c41d8",
                  "id": "CONSERVAZIONE LINEA1 B",
                  "name": "CONSERVAZIONE LINEA1 B",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 28,
                  "h": 1
                }
              ]
            },
            {
              "_uid": "53ae26cb-34f2-483a-bad3-18c63fe81b29",
              "id": "R11024",
              "name": "Rack R11024",
              "row": "A",
              "u": 45,
              "x": 5.97,
              "y": 0.15,
              "w": 0.63,
              "h": 0.72,
              "seriali": [
                "2008034260"
              ],
              "devices": [
                {
                  "_uid": "ec688cd7-7b02-4bf7-9bda-f43dde0ea587",
                  "id": "DR-ESX03",
                  "name": "DR-ESX03",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 44,
                  "h": 1
                },
                {
                  "_uid": "2ad142a6-16a7-4a81-975e-a7f8afaee52b",
                  "id": "DR-PECESXI1",
                  "name": "DR-PECESXI1",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 42,
                  "h": 1
                },
                {
                  "_uid": "2c1573b8-a472-431c-8cd1-52a6a7d77bb3",
                  "id": "FA-BE2 (Banca Sella)",
                  "name": "FA-BE2 (Banca Sella)",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 40,
                  "h": 1
                },
                {
                  "_uid": "5f234afb-2185-4397-917e-a64d53b21ba2",
                  "id": "RDBMS",
                  "name": "RDBMS",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 38,
                  "h": 1
                },
                {
                  "_uid": "cd7d0695-df03-46f3-b077-93afdafcb76c",
                  "id": "OPTISERVER-DR",
                  "name": "OPTISERVER-DR",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 36,
                  "h": 1
                },
                {
                  "_uid": "ea271096-9a3e-4bb5-9864-abb74dad7702",
                  "id": "CONSERVAZIONE LINEA2 A",
                  "name": "CONSERVAZIONE LINEA2 A",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 34,
                  "h": 1
                },
                {
                  "_uid": "86e694b8-dfb2-4d2a-9a75-a9166780c2e9",
                  "id": "CONSERVAZIONE LINEA2 B",
                  "name": "CONSERVAZIONE LINEA2 B",
                  "type": "server",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 32,
                  "h": 1
                }
              ]
            },
            {
              "_uid": "16569078-1f6b-408e-a952-20c1492ef202",
              "id": "RO-RETE",
              "name": "Rack RO-RETE",
              "row": "A",
              "u": 45,
              "x": 6.6,
              "y": 0.15,
              "w": 0.63,
              "h": 0.72,
              "seriali": [
                "2012170418"
              ],
              "devices": [
                {
                  "_uid": "f2a9fb57-f458-4861-b93d-ed5ccc23434a",
                  "id": "SWITCH CISCO C-1 1",
                  "name": "SWITCH CISCO C-1 1",
                  "type": "rete",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 44,
                  "h": 1
                },
                {
                  "_uid": "aa93e358-764b-4314-96f6-94b2ab85cb5d",
                  "id": "SWITCH CISCO C-1 2",
                  "name": "SWITCH CISCO C-1 2",
                  "type": "rete",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 42,
                  "h": 1
                },
                {
                  "_uid": "13a23f59-5d74-4fa5-87f3-71255f22823b",
                  "id": "SWITCH CISCO C-1 3",
                  "name": "SWITCH CISCO C-1 3",
                  "type": "rete",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 40,
                  "h": 1
                },
                {
                  "_uid": "b15f2b8d-d83c-47ea-bc4e-7702b5698e61",
                  "id": "ROUTER HYPERWAY",
                  "name": "ROUTER HYPERWAY",
                  "type": "rete",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 38,
                  "h": 1
                },
                {
                  "_uid": "591428f8-dced-4cae-8fb4-60df5de955ca",
                  "id": "SWITCH DRCORE1",
                  "name": "SWITCH DRCORE1",
                  "type": "rete",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 36,
                  "h": 1
                },
                {
                  "_uid": "650ff62d-f8dd-468c-8f8b-9898bc946218",
                  "id": "RADWARE 4208 1",
                  "name": "RADWARE 4208 1",
                  "type": "rete",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 34,
                  "h": 1
                },
                {
                  "_uid": "13c60b35-df37-4867-adfa-b0478beb690d",
                  "id": "RADWARE 4208 2",
                  "name": "RADWARE 4208 2",
                  "type": "rete",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 32,
                  "h": 1
                }
              ]
            },
            {
              "_uid": "20f424a8-967f-4f20-b125-c975ae7fac20",
              "id": "R11115",
              "name": "Rack R11115",
              "row": "B",
              "u": 45,
              "x": 0.615,
              "y": 5.1,
              "w": 0.63,
              "h": 0.72,
              "seriali": [
                "2012170515"
              ],
              "devices": []
            },
            {
              "_uid": "120f775d-92ca-4eef-b3ac-c436986db397",
              "id": "RO4495",
              "name": "Rack RO4495",
              "row": "B",
              "u": 45,
              "x": 1.245,
              "y": 5.1,
              "w": 0.63,
              "h": 0.72,
              "seriali": [
                "2004126690",
                "2020044487"
              ],
              "devices": [
                {
                  "_uid": "d40a3e4d-1118-438f-8a1c-e7cf8062f031",
                  "id": "VNX 2",
                  "name": "VNX 2",
                  "type": "storage",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 41,
                  "h": 4
                }
              ]
            },
            {
              "_uid": "6742af57-e845-4b7f-bda6-f21868a015b2",
              "id": "RO-B03",
              "name": "Rack RO-B03",
              "row": "B",
              "u": 45,
              "x": 1.875,
              "y": 5.1,
              "w": 0.63,
              "h": 0.72,
              "seriali": [
                "2020062936",
                "2020062935"
              ],
              "devices": [
                {
                  "_uid": "1c70e6dc-b022-42d9-a173-d4e6b4123165",
                  "id": "ISILON 2",
                  "name": "ISILON 2",
                  "type": "storage",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 41,
                  "h": 4
                }
              ]
            },
            {
              "_uid": "c4119199-8dca-49a3-93f6-fee164c66392",
              "id": "RO-B04",
              "name": "Rack RO-B04",
              "row": "B",
              "u": 45,
              "x": 5.655,
              "y": 5.1,
              "w": 0.63,
              "h": 0.72,
              "seriali": [
                "2006029736"
              ],
              "devices": [
                {
                  "_uid": "6946e3f8-f612-429b-b718-50c0240b39bd",
                  "id": "ISILON 3",
                  "name": "ISILON 3",
                  "type": "storage",
                  "model": "",
                  "ip": "",
                  "serial": "",
                  "owner": "",
                  "u": 41,
                  "h": 4
                }
              ]
            },
            {
              "_uid": "6c731e37-9f78-493e-baba-e5a81980a60b",
              "id": "RO-B05",
              "name": "Rack RO-B05",
              "row": "B",
              "u": 45,
              "x": 6.285,
              "y": 5.1,
              "w": 0.63,
              "h": 0.72,
              "seriali": [
                "2006004089"
              ],
              "devices": []
            }
          ]
        }
      ]
    }
  ],
  "schemaVersion": 1
};
