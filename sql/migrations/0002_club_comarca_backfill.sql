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
    ('BARÇA', 'Barcelona', 'Barcelona', 'Barcelonès'),
    ('ADISS HOCKEY RIVAS', 'Rivas-Vaciamadrid', 'Madrid', NULL),
    ('IGUALADA RIGAT HC', 'Igualada', 'Barcelona', 'Anoia'),
    ('CP VOLTREGA MOVIMENTO STERN', 'Vic', 'Barcelona', 'Osona'),
    ('INNOAESTHETICS HC SANT JUST', 'Sant Just Desvern', 'Barcelona', 'Baix Llobregat'),
    ('SHUM FRIT RAVICH', 'Manresa', 'Barcelona', 'Bages'),
    ('HOCKEY CLUB LICEO', 'la Corunya', 'la Corunya', NULL),
    ('AITEX PAS ALCOI', 'Alcoi', 'Alacant', NULL),
    ('REUS DEPORTIU BRASILIA', 'Reus', 'Tarragona', 'Baix Camp'),
    ('PONS LLEIDA', 'Lleida', 'Lleida', 'Segrià'),
    ('CE NOIA FREIXENET', 'Sant Sadurní d''Anoia', 'Barcelona', 'Alt Penedès'),
    ('CH CALDES RECAM LÀSER', 'Caldes de Montbui', 'Barcelona', 'Vallès Oriental'),
    ('CALAFELL LA MENORQUINA', 'Calafell', 'Tarragona', 'Baix Penedès'),
    ('CERDANYOLA CLUB D''HOQUEI', 'Cerdanyola del Vallès', 'Barcelona', 'Vallès Occidental')
) AS v(canonical_name, city, province, comarca)
WHERE club.canonical_name = v.canonical_name
  AND club.comarca IS NULL;
