import gzip, json, os, sys, types, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# синтетический справочник в формате bn-auto
rows = {
 "Корея|hyundai|grandeur": [
   [2026,2100,1598,180,0,0,"h","Передний","HEV 1.6 AT 2WD Premium"],
   [2012,2014,3342,294,0,0,"p","Передний","3.3 GDI AT 2WD HG330 Celebrity"],
   [2012,2014,2999,270,0,0,"p","Передний","3.0 GDI AT 2WD HG300 Premium"]],
 "Корея|hyundai|avante": [
   [2020,2023,1598,123,0,0,"p","Передний","1.6 MPI"],
   [2020,2023,1598,123,0,0,"p","Передний","1.6 MPI Smart"],
   [2020,2023,1598,204,0,0,"p","Передний","1.6 T-GDI N Line"]],
 "Корея|kia|k5": [[2020,2100,1999,160,0,0,"p","Передний","2.0 MPI"],[2020,2100,1999,160,0,0,"p","Полный","2.0 MPI AWD"]],
 "Корея|hyundai|ioniq5": [[2021,2100,0,229,0,0,"e","Задний","Long Range 2WD"],[2021,2100,0,325,0,0,"e","Полный","Long Range AWD"]],
 "Корея|renaultsamsung|sm6": [[2016,2020,1998,150,0,0,"p","Передний","LE"]],
 "Корея|ssangyong|tivoli": [[2015,2022,1597,128,0,0,"p","Передний","1.6"]],
}
d = tempfile.mkdtemp(); path = os.path.join(d, "i.json.gz")
with gzip.open(path, "wt", encoding="utf-8") as f: json.dump(rows, f, ensure_ascii=False)
os.environ["DROM_INDEX_PATH"] = path
import drom_index as di
I = di.load()
m = I.match("Корея","Hyundai","Grandeur",2026,"гибрид (бензин + электро)",1598); assert m["status"]=="ok" and m["hp"]==180, m
assert I.match("Корея","Hyundai","Grandeur",2013,"бензин",3342)["hp"]==294
assert I.match("Корея","Hyundai","Grandeur",2013,"бензин",None)["status"]=="ambiguous"
assert I.match("Корея","Hyundai","Avante",2021,"бензин",1598)["status"]=="ambiguous"      # 123 и 204
assert I.match("Корея","Hyundai","Avante",2021,"бензин",1598,power=123)["hp"]==123         # сужение по мощности
assert I.match("Корея","Hyundai","Avante",2019,"бензин",1598)["status"]=="none"            # год вне периода
assert I.match("Корея","Hyundai","Nonexistent",2021,"бензин",1598)["reason"]=="model"
assert I.match("Корея","Kia","K5",2021,"бензин",1999,"полный")["drive"]=="Полный"
assert I.match("Корея","Kia","K5",2021,"бензин",1999)["hp"]==160                           # привод разный, мощность одна
assert I.power_class("Корея","Kia","K5",2021,"бензин",1999)=="le160"
assert I.power_class("Корея","Hyundai","Ioniq 5",2022,"электро",None,"полный")=="gt160"     # электро — всегда «любой мощности»
assert I.power_class("Корея","Hyundai","Ioniq 5",2022,"электро")  is None                   # 229/325 — неоднозначно
assert I.power_band("Корея","Hyundai","Ioniq 5",2022,"e")=="gt160"
assert I.power_band("Корея","Kia","K5",2021,"p")=="le160"
assert I.power_band("Корея","Hyundai","Avante",2021,"p") is None                          # 123 и 204 → нужна карточка
assert I.power_band("Корея","Hyundai","Grandeur",2013,"p")=="gt160"
assert I.match("Корея","Renault","SM6",2018,"бензин",1998)["hp"]==150                      # Renault → renaultsamsung
assert I.match("Корея","KGM","Tivoli",2020,"бензин",1597)["hp"]==128                       # KGM → ssangyong
assert I.match("Япония","Toyota","Corolla",2020,"бензин",1800)["reason"]=="model"           # другой рынок — нет
assert di.fuel_code("гибрид (дизель + электро)")=="h" and di.fuel_code("электро")=="e" and di.fuel_code("газ (LPG)")=="p"
assert di.model_keys("Volkswagen","Passat Variant")[:3]==["passatvariant","volkswagenpassatvariant","passat"]
print("drom_index OK", I.stats, dict(I.missing))

# --- run_drom с подставленными scraper / coverage / selection ---
sel = types.ModuleType("selection"); sel.YEAR_BANDS=[("x",0,0,1.0)]; sel.eligible=lambda b,y: bool(b and y and y>=2010)
cov = types.ModuleType("coverage"); cov._to_car=lambda r: {"external_id":"1","brand_en":"Kia","model_guess":"K5","year":2021}; cov._resolver=None
scr = types.ModuleType("scraper"); scr.main=lambda: print("main()")
for n,mod in (("selection",sel),("coverage",cov),("scraper",scr)): sys.modules[n]=mod
import run_drom as R
assert [b[0] for b in sel.YEAR_BANDS]==["2022–2025","2010–2018","2019–2021","2026"] and abs(sum(b[3] for b in sel.YEAR_BANDS)-1)<1e-9, sel.YEAR_BANDS
assert os.environ["ENCAR_SHARE_160"]=="0.70"
assert cov._to_car({"FuelType":"가솔린"})["power"]=="le160"
# строгий класс по карточке
d_ok = {"make":"Hyundai","model":"Avante","year":2021,"displacement":1598,"spec":{"Топливо":"бензин"}}
assert R.class_for_detail(d_ok) is None
d_ok["spec"]["Топливо"]="бензин"; d_ok2=dict(d_ok, model="K5", make="Kia", displacement=1999); assert R.class_for_detail(d_ok2)=="le160"
class P: blocked=False
res = cov._resolver(None, {"9":{"hp":"204","engine_type":"petrol"}}, P(), None, None)
assert res({"external_id":"9"})=="gt160"
# add_drom_power
car={"external_id":"5","detail":{"make":"Kia","model":"K5","year":2021,"displacement":1999,"spec":{"Топливо":"бензин","Привод":"полный"}}}
counts={"found":0,"none":0}; tried={}
scr.add_drom_power([car], None, counts, tried)
assert car["detail"]["spec"]["Мощность, л.с."]=="160" and counts["found"]==1, car
bad={"external_id":"6","detail":{"make":"Hyundai","model":"Avante","year":2021,"displacement":1598,"spec":{"Топливо":"бензин"}}}
scr.add_drom_power([bad], None, counts, tried); assert counts["none"]==1 and "6" in tried and "Мощность, л.с." not in bad["detail"]["spec"]
assert scr.still_ok(car) and not scr.still_ok(bad)
print("run_drom OK")
