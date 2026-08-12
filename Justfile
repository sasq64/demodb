
pages:
    test -e pages || 7z x bitworld_pages.7z

csdb_cache:
    test -e .csdb_cache || 7z x csdb_cache.7z

demozoo_export:
    test -e demozoo-export.sql || ( wget https://data.demozoo.org/demozoo-export.sql.gz && gunzip demozoo-export.sql.gz)

bitworld: pages
    ./bitworld_gen.py -o bitworld.txt 1 111667

csdb: csdb_cache
    ./csdb.py

demozoo: demozoo_export
    ./demozoo.py


export:
    gzip -f csdb.txt
    gzip -f demozoo.txt
    cp *.gz ../docs/minnberg/static/dl
