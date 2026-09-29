-- Migració: omple la comarca (i província/ciutat si falten) dels clubs
-- de la Parlem OK Lliga 2025/26.
-- SEGUR per a bases amb valors editats a mà: només actualitza els camps
-- que encara són NULL, mai sobreescriu dades existents.
-- Idempotent: es pot reexecutar.

UPDATE club SET
    comarca = v.comarca,
    province = COALESCE(club.province, v.province),
    city = COALESCE(club.city, v.city)
FROM (VALUES
    ('FC BARCELONA', 'Barcelona', 'Barcelona', 'Barcelonès'),
    ('ADISS HOCKEY RIVAS', 'Rivas-Vaciamadrid', 'Madrid', NULL),
    ('IGUALADA HC', 'Igualada', 'Barcelona', 'Anoia'),
    ('CP VOLTREGA', 'Vic', 'Barcelona', 'Osona'),
    ('HC SANT JUST', 'Sant Just Desvern', 'Barcelona', 'Baix Llobregat'),
    ('SHUM MAÇANET', 'Maçanet de la Selva', 'Girona', 'Selva'),
    ('HOCKEY CLUB LICEO', 'la Corunya', 'la Corunya', NULL),
    ('AITEX PAS ALCOI', 'Alcoi', 'Alacant', NULL),
    ('REUS DEPORTIU', 'Reus', 'Tarragona', 'Baix Camp'),
    ('LLEIDA LLISTA BLAVA', 'Lleida', 'Lleida', 'Segrià'),
    ('CE NOIA FREIXENET', 'Sant Sadurní d''Anoia', 'Barcelona', 'Alt Penedès'),
    ('CH CALDES', 'Caldes de Montbui', 'Barcelona', 'Vallès Oriental'),
    ('CP CALAFELL', 'Calafell', 'Tarragona', 'Baix Penedès'),
    ('CERDANYOLA CLUB D''HOQUEI', 'Cerdanyola del Vallès', 'Barcelona', 'Vallès Occidental')
) AS v(canonical_name, city, province, comarca)
WHERE club.canonical_name = v.canonical_name
  AND club.comarca IS NULL;
