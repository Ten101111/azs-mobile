-- Что нужно, чтобы перевести ИИ-контур на витрины dm.data_for_ai_analytic_part_1/2.
-- Все запросы только читают. Результат каждого прислать целиком.

-- 1. СТОЛБЦЫ ОБЕИХ ВИТРИН — главное.
--    Из этого собирается каталог валидатора и описание схемы для модели.
SELECT table_name AS "витрина",
       ordinal_position AS "№",
       column_name AS "столбец",
       data_type AS "тип",
       is_nullable AS "пусто?"
FROM information_schema.columns
WHERE table_schema = 'dm'
  AND table_name IN ('data_for_ai_analytic_part_1', 'data_for_ai_analytic_part_2')
ORDER BY table_name, ordinal_position;

-- 2. ГРАНУЛЯРНОСТЬ И ГЛУБИНА ИСТОРИИ.
--    Подставьте вместо <дата> и <ксс> реальные имена из шага 1.
--    Нужно понять: строка — это АЗС за день или АЗС за месяц.
SELECT COUNT(*)                        AS "строк",
       COUNT(DISTINCT <ксс>)           AS "объектов",
       MIN(<дата>)                     AS "история с",
       MAX(<дата>)                     AS "история по",
       COUNT(*)::numeric
         / NULLIF(COUNT(DISTINCT <ксс>), 0) AS "строк на объект"
FROM dm.data_for_ai_analytic_part_1;

-- 3. КАК СВЯЗАНЫ ДВЕ ВИТРИНЫ.
--    Совпадают ли ключи и период; можно ли соединять по объекту и дате.
SELECT (SELECT COUNT(DISTINCT <ксс>) FROM dm.data_for_ai_analytic_part_1) AS "объектов в part_1",
       (SELECT COUNT(DISTINCT <ксс>) FROM dm.data_for_ai_analytic_part_2) AS "объектов в part_2",
       (SELECT MAX(<дата>) FROM dm.data_for_ai_analytic_part_1)           AS "последняя дата part_1",
       (SELECT MAX(<дата>) FROM dm.data_for_ai_analytic_part_2)           AS "последняя дата part_2";

-- 4. ОДНА СТРОКА ЦЕЛИКОМ ДЛЯ ПОНИМАНИЯ ФОРМАТА ЗНАЧЕНИЙ.
--    Даёт увидеть, как выглядят коды, даты, разделители и единицы.
--    Персональных данных в витринах быть не должно; если в строке окажется
--    что-то личное — не присылайте, просто скажите, в каком столбце.
SELECT * FROM dm.data_for_ai_analytic_part_1 LIMIT 1;
SELECT * FROM dm.data_for_ai_analytic_part_2 LIMIT 1;
