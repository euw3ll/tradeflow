import os

# --- Configuração ---
# Diretório raiz do seu projeto (onde este script será colocado)
PROJECT_ROOT = '.'

# Nome do arquivo de saída
OUTPUT_FILE = 'codigo_do_projeto.txt'

# Pastas que serão ignoradas na extração
EXCLUDE_DIRS = {'.venv', '.git', '.vscode', '__pycache__', '.github'}

# Extensões de arquivo a serem incluídas
INCLUDE_EXTENSIONS = {'.py', '.sh', 'Dockerfile', '.toml'}

# --- Fim da Configuração ---

def should_exclude(path, exclude_set):
    """Verifica se um caminho ou um de seus pais está no conjunto de exclusão."""
    parts = path.split(os.sep)
    return any(part in exclude_set for part in parts)

def has_valid_extension(filename, extensions):
    """Verifica se o nome do arquivo termina com uma das extensões válidas."""
    if filename in extensions: # Para nomes de arquivo exatos como 'Dockerfile'
        return True
    return any(filename.endswith(ext) for ext in extensions if ext.startswith('.'))

def extract_project_code():
    """
    Varre o diretório do projeto, lê o conteúdo dos arquivos de código
    e os salva em um único arquivo de texto.
    """
    try:
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as outfile:
            print(f'Criando o arquivo de saída "{OUTPUT_FILE}"...')
            
            for root, dirs, files in os.walk(PROJECT_ROOT, topdown=True):
                # Otimização: remove as pastas excluídas da próxima varredura do os.walk
                dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]

                for filename in sorted(files):
                    file_path = os.path.join(root, filename)
                    relative_path = os.path.relpath(file_path, PROJECT_ROOT)

                    # Pula o próprio script extrator e arquivos em pastas excluídas
                    if filename == os.path.basename(__file__) or should_exclude(relative_path, EXCLUDE_DIRS):
                        continue

                    if has_valid_extension(filename, INCLUDE_EXTENSIONS):
                        try:
                            with open(file_path, 'r', encoding='utf-8') as infile:
                                content = infile.read()
                                
                                # Escreve um cabeçalho para cada arquivo no output
                                header = f'{"="*40}\n# Arquivo: {relative_path}\n{"="*40}\n\n'
                                outfile.write(header)
                                outfile.write(content)
                                outfile.write('\n\n')
                                print(f'-> Adicionado: {relative_path}')

                        except Exception as e:
                            error_message = f'# ERRO ao ler o arquivo {relative_path}: {e}\n\n'
                            outfile.write(error_message)
                            print(f'[!] Erro ao ler {relative_path}: {e}')

        print(f'\nExtração concluída com sucesso! Todo o código foi salvo em "{OUTPUT_FILE}".')

    except IOError as e:
        print(f'[!] Erro de E/S: Não foi possível escrever no arquivo {OUTPUT_FILE}. Razão: {e}')
    except Exception as e:
        print(f'[!] Ocorreu um erro inesperado: {e}')


if __name__ == '__main__':
    extract_project_code()